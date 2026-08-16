"""SQLite persistence for summaries and for the entry content we replaced.

The original Miniflux content is kept here so a rewritten entry can always be
rebuilt from scratch. That makes write-back idempotent no matter how many times
an entry is summarized, and lets the change be undone.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS summaries (
    entry_id     INTEGER PRIMARY KEY,
    url          TEXT NOT NULL,
    status       TEXT NOT NULL,
    source       TEXT,
    payload      TEXT,
    error        TEXT,
    model        TEXT,
    original     TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
"""

# Columns added after a release, applied on open to databases that predate them.
MIGRATIONS = (("attempts", "ALTER TABLE summaries ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"),)

PENDING = "pending"
READY = "ready"
ERROR = "error"


@dataclass(slots=True)
class Summary:
    entry_id: int
    url: str
    status: str
    source: str | None = None
    payload: dict[str, Any] | None = None
    error: str | None = None
    model: str | None = None

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"entry_id": self.entry_id, "status": self.status}
        if self.source:
            body["source"] = self.source
        if self.model:
            body["model"] = self.model
        if self.payload:
            body.update(self.payload)
        if self.error:
            body["error"] = self.error
        return body


class Store:
    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        present = {row["name"] for row in self._db.execute("PRAGMA table_info(summaries)")}
        for column, statement in MIGRATIONS:
            if column not in present:
                self._db.execute(statement)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _row_to_summary(self, row: sqlite3.Row) -> Summary:
        return Summary(
            entry_id=row["entry_id"],
            url=row["url"],
            status=row["status"],
            source=row["source"],
            payload=json.loads(row["payload"]) if row["payload"] else None,
            error=row["error"],
            model=row["model"],
        )

    def get(self, entry_id: int) -> Summary | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM summaries WHERE entry_id = ?", (entry_id,)).fetchone()
        return self._row_to_summary(row) if row else None

    def statuses(self, entry_ids: list[int]) -> dict[int, str]:
        """Status for many entries at once, so a page needs one round trip."""

        if not entry_ids:
            return {}
        placeholders = ",".join("?" * len(entry_ids))
        with self._lock:
            rows = self._db.execute(
                f"SELECT entry_id, status FROM summaries WHERE entry_id IN ({placeholders})",
                entry_ids,
            ).fetchall()
        return {row["entry_id"]: row["status"] for row in rows}

    def claim(self, entry_id: int, url: str, *, force: bool = False) -> tuple[Summary, bool]:
        """Return the stored summary, creating a pending row when work is needed.

        The boolean says whether the caller must now queue the job. A row that is
        already pending is never queued twice, so overlapping requests for the
        same entry collapse into one run.
        """

        now = time.time()
        with self._lock:
            row = self._db.execute("SELECT * FROM summaries WHERE entry_id = ?", (entry_id,)).fetchone()
            if row is not None and not force and row["status"] in (READY, PENDING):
                return self._row_to_summary(row), False
            self._db.execute(
                """
                INSERT INTO summaries (entry_id, url, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(entry_id) DO UPDATE SET
                    url = excluded.url,
                    status = excluded.status,
                    source = NULL,
                    payload = NULL,
                    error = NULL,
                    model = NULL,
                    attempts = 0,
                    updated_at = excluded.updated_at
                """,
                (entry_id, url, PENDING, now, now),
            )
            self._db.commit()
        return Summary(entry_id=entry_id, url=url, status=PENDING), True

    def finish(
        self,
        entry_id: int,
        *,
        status: str,
        source: str | None = None,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
        model: str | None = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                """
                UPDATE summaries
                   SET status = ?, source = ?, payload = ?, error = ?, model = ?, updated_at = ?
                 WHERE entry_id = ?
                """,
                (
                    status,
                    source,
                    json.dumps(payload, ensure_ascii=False) if payload else None,
                    error,
                    model,
                    time.time(),
                    entry_id,
                ),
            )
            self._db.commit()

    def bump_attempts(self, entry_id: int) -> int:
        """Count one run of an entry and return how many have now been made."""

        with self._lock:
            self._db.execute(
                "UPDATE summaries SET attempts = attempts + 1, updated_at = ? WHERE entry_id = ?",
                (time.time(), entry_id),
            )
            row = self._db.execute(
                "SELECT attempts FROM summaries WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            self._db.commit()
        return row["attempts"] if row else 0

    def pending_entries(self, limit: int) -> list[tuple[int, str]]:
        """Entries still marked pending, oldest first.

        On a clean run this is empty. After a restart it holds the jobs that were
        in flight or waiting to be retried, which nothing else would ever pick up
        again: `claim` treats a pending row as work already under way.
        """

        with self._lock:
            rows = self._db.execute(
                "SELECT entry_id, url FROM summaries WHERE status = ? ORDER BY updated_at LIMIT ?",
                (PENDING, limit),
            ).fetchall()
        return [(row["entry_id"], row["url"]) for row in rows]

    def abandon_pending(self, error: str, *, keep: list[int]) -> int:
        """Fail every pending row except those being resumed, and count them."""

        # `NOT IN ()` is not valid SQL, so an empty exception list drops the clause.
        exclusion = f" AND entry_id NOT IN ({','.join('?' * len(keep))})" if keep else ""
        with self._lock:
            cursor = self._db.execute(
                "UPDATE summaries SET status = ?, error = ?, updated_at = ?"
                f" WHERE status = ?{exclusion}",
                (ERROR, error, time.time(), PENDING, *keep),
            )
            self._db.commit()
        return cursor.rowcount

    def original_content(self, entry_id: int) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT original FROM summaries WHERE entry_id = ?", (entry_id,)).fetchone()
        return row["original"] if row else None

    def remember_original(self, entry_id: int, content: str) -> str:
        """Store the pre-summary content once and return the copy to build from."""

        with self._lock:
            row = self._db.execute("SELECT original FROM summaries WHERE entry_id = ?", (entry_id,)).fetchone()
            if row is not None and row["original"] is not None:
                return row["original"]
            self._db.execute(
                "UPDATE summaries SET original = ?, updated_at = ? WHERE entry_id = ?",
                (content, time.time(), entry_id),
            )
            self._db.commit()
        return content
