"""Generation-safe SQLite persistence for summaries and Miniflux write-back."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS summaries (
    entry_id           INTEGER PRIMARY KEY CHECK (entry_id > 0),
    url                TEXT NOT NULL,
    status             TEXT NOT NULL,
    source             TEXT,
    payload            TEXT,
    error              TEXT,
    model              TEXT,
    original           TEXT,
    attempts           INTEGER NOT NULL DEFAULT 0,
    generation         INTEGER NOT NULL DEFAULT 0,
    summary_html       TEXT,
    writeback_status   TEXT NOT NULL DEFAULT 'disabled',
    writeback_error    TEXT,
    writeback_attempts INTEGER NOT NULL DEFAULT 0,
    retry_at           REAL,
    auto_retries       INTEGER NOT NULL DEFAULT 0,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
);
"""

# Migrations are deliberately additive so the exact legacy schema upgrades in
# place without rebuilding the table or risking its saved originals.
MIGRATIONS = (
    ("attempts", "ALTER TABLE summaries ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"),
    ("generation", "ALTER TABLE summaries ADD COLUMN generation INTEGER NOT NULL DEFAULT 0"),
    ("summary_html", "ALTER TABLE summaries ADD COLUMN summary_html TEXT"),
    (
        "writeback_status",
        "ALTER TABLE summaries ADD COLUMN writeback_status TEXT NOT NULL DEFAULT 'disabled'",
    ),
    ("writeback_error", "ALTER TABLE summaries ADD COLUMN writeback_error TEXT"),
    (
        "writeback_attempts",
        "ALTER TABLE summaries ADD COLUMN writeback_attempts INTEGER NOT NULL DEFAULT 0",
    ),
    ("retry_at", "ALTER TABLE summaries ADD COLUMN retry_at REAL"),
    ("auto_retries", "ALTER TABLE summaries ADD COLUMN auto_retries INTEGER NOT NULL DEFAULT 0"),
)

PENDING = "pending"
READY = "ready"
ERROR = "error"

WRITEBACK_DISABLED = "disabled"
WRITEBACK_PENDING = "pending"
WRITEBACK_SUCCEEDED = "successful"
WRITEBACK_FAILED = "failed"


@dataclass(slots=True)
class Summary:
    entry_id: int
    url: str
    status: str
    generation: int
    source: str | None = None
    payload: dict[str, Any] | None = None
    error: str | None = None
    model: str | None = None
    summary_html: str | None = None
    writeback_status: str = WRITEBACK_DISABLED
    writeback_error: str | None = None
    writeback_attempts: int = 0
    # When a failed entry will be started over automatically; None means never.
    retry_at: float | None = None
    auto_retries: int = 0

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
        if self.retry_at:
            body["retry_at"] = self.retry_at
        body["write_back"] = {
            "status": self.writeback_status,
            "attempts": self.writeback_attempts,
        }
        if self.writeback_error:
            body["write_back"]["error"] = self.writeback_error
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
        if "retry_at" not in present:
            # Failures from before automatic retrying existed get one more round.
            self._db.execute(
                "UPDATE summaries SET retry_at = ? WHERE status = ?", (time.time(), ERROR)
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _row_to_summary(row: sqlite3.Row) -> Summary:
        return Summary(
            entry_id=row["entry_id"],
            url=row["url"],
            status=row["status"],
            generation=row["generation"],
            source=row["source"],
            payload=json.loads(row["payload"]) if row["payload"] else None,
            error=row["error"],
            model=row["model"],
            summary_html=row["summary_html"],
            writeback_status=row["writeback_status"],
            writeback_error=row["writeback_error"],
            writeback_attempts=row["writeback_attempts"],
            retry_at=row["retry_at"],
            auto_retries=row["auto_retries"],
        )

    def get(self, entry_id: int) -> Summary | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM summaries WHERE entry_id = ?", (entry_id,)
            ).fetchone()
        return self._row_to_summary(row) if row else None

    def is_current(self, entry_id: int, generation: int) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM summaries WHERE entry_id = ? AND generation = ?",
                (entry_id, generation),
            ).fetchone()
        return row is not None

    def statuses(self, entry_ids: list[int]) -> dict[int, str]:
        if not entry_ids:
            return {}
        placeholders = ",".join("?" * len(entry_ids))
        with self._lock:
            rows = self._db.execute(
                f"SELECT entry_id, status FROM summaries WHERE entry_id IN ({placeholders})",
                entry_ids,
            ).fetchall()
        return {row["entry_id"]: row["status"] for row in rows}

    def claim(
        self, entry_id: int, url: str, *, force: bool = False, automatic: bool = False
    ) -> tuple[Summary, bool]:
        """Claim fresh work and assign it a new monotonically increasing generation.

        An automatic claim counts towards the entry's automatic retries; any
        other claim is someone asking afresh, so the count starts over.
        """

        now = time.time()
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM summaries WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            if row is not None and not force and row["status"] in (READY, PENDING):
                return self._row_to_summary(row), False

            if row is None:
                generation = 1
                self._db.execute(
                    """
                    INSERT INTO summaries
                        (entry_id, url, status, generation, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (entry_id, url, PENDING, generation, now, now),
                )
            else:
                generation = row["generation"] + 1
                self._db.execute(
                    """
                    UPDATE summaries
                       SET url = ?, status = ?, source = NULL, payload = NULL,
                           error = NULL, model = NULL, attempts = 0,
                           generation = ?, summary_html = NULL,
                           writeback_status = ?, writeback_error = NULL,
                           writeback_attempts = 0, retry_at = NULL, auto_retries = ?,
                           updated_at = ?
                     WHERE entry_id = ?
                    """,
                    (
                        url,
                        PENDING,
                        generation,
                        WRITEBACK_DISABLED,
                        row["auto_retries"] + 1 if automatic else 0,
                        now,
                        entry_id,
                    ),
                )
            result = self._db.execute(
                "SELECT * FROM summaries WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            self._db.commit()
        return self._row_to_summary(result), True

    def finish(
        self,
        entry_id: int,
        *,
        status: str,
        generation: int | None = None,
        source: str | None = None,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
        model: str | None = None,
        retry_at: float | None = None,
    ) -> bool:
        """Conditionally finish summary work; false means the generation went stale."""

        condition = "entry_id = ?" + (" AND generation = ?" if generation is not None else "")
        values: tuple[Any, ...] = (entry_id, generation) if generation is not None else (entry_id,)
        with self._lock:
            cursor = self._db.execute(
                f"""
                UPDATE summaries
                   SET status = ?, source = ?, payload = ?, error = ?, model = ?,
                       retry_at = ?, updated_at = ?
                 WHERE {condition}
                """,
                (
                    status,
                    source,
                    json.dumps(payload, ensure_ascii=False) if payload else None,
                    error,
                    model,
                    retry_at,
                    time.time(),
                    *values,
                ),
            )
            self._db.commit()
        return cursor.rowcount == 1

    def complete_summary(
        self,
        entry_id: int,
        generation: int,
        *,
        source: str,
        model: str,
        payload: dict[str, Any],
        summary_html: str,
        write_back: bool,
    ) -> bool:
        """Persist the usable summary before any external write is attempted."""

        with self._lock:
            cursor = self._db.execute(
                """
                UPDATE summaries
                   SET status = ?, source = ?, payload = ?, error = NULL, model = ?,
                       summary_html = ?, writeback_status = ?, writeback_error = NULL,
                       writeback_attempts = 0, updated_at = ?
                 WHERE entry_id = ? AND generation = ? AND status = ?
                """,
                (
                    READY,
                    source,
                    json.dumps(payload, ensure_ascii=False),
                    model,
                    summary_html,
                    WRITEBACK_PENDING if write_back else WRITEBACK_DISABLED,
                    time.time(),
                    entry_id,
                    generation,
                    PENDING,
                ),
            )
            self._db.commit()
        return cursor.rowcount == 1

    def bump_attempts(self, entry_id: int, generation: int | None = None) -> int | None:
        condition = "entry_id = ?" + (" AND generation = ?" if generation is not None else "")
        values: tuple[Any, ...] = (entry_id, generation) if generation is not None else (entry_id,)
        with self._lock:
            cursor = self._db.execute(
                f"UPDATE summaries SET attempts = attempts + 1, updated_at = ? WHERE {condition}",
                (time.time(), *values),
            )
            if cursor.rowcount != 1:
                self._db.commit()
                return None
            row = self._db.execute(
                f"SELECT attempts FROM summaries WHERE {condition}", values
            ).fetchone()
            self._db.commit()
        return row["attempts"]

    def bump_writeback_attempts(self, entry_id: int, generation: int) -> int | None:
        with self._lock:
            cursor = self._db.execute(
                """
                UPDATE summaries
                   SET writeback_attempts = writeback_attempts + 1, updated_at = ?
                 WHERE entry_id = ? AND generation = ? AND writeback_status = ?
                """,
                (time.time(), entry_id, generation, WRITEBACK_PENDING),
            )
            if cursor.rowcount != 1:
                self._db.commit()
                return None
            row = self._db.execute(
                "SELECT writeback_attempts FROM summaries WHERE entry_id = ? AND generation = ?",
                (entry_id, generation),
            ).fetchone()
            self._db.commit()
        return row["writeback_attempts"]

    def finish_writeback(
        self, entry_id: int, generation: int, *, successful: bool, error: str | None = None
    ) -> bool:
        with self._lock:
            cursor = self._db.execute(
                """
                UPDATE summaries
                   SET writeback_status = ?, writeback_error = ?, updated_at = ?
                 WHERE entry_id = ? AND generation = ? AND writeback_status = ?
                """,
                (
                    WRITEBACK_SUCCEEDED if successful else WRITEBACK_FAILED,
                    None if successful else error,
                    time.time(),
                    entry_id,
                    generation,
                    WRITEBACK_PENDING,
                ),
            )
            self._db.commit()
        return cursor.rowcount == 1

    def pending_entries(self, limit: int) -> list[tuple[int, str, int]]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT entry_id, url, generation FROM summaries
                 WHERE status = ? ORDER BY updated_at LIMIT ?
                """,
                (PENDING, limit),
            ).fetchall()
        return [(row["entry_id"], row["url"], row["generation"]) for row in rows]

    def pending_writebacks(self, limit: int | None = None) -> list[tuple[int, str, int]]:
        limit_clause = " LIMIT ?" if limit is not None else ""
        parameters: tuple[Any, ...] = (
            (READY, WRITEBACK_PENDING, limit) if limit is not None else (READY, WRITEBACK_PENDING)
        )
        with self._lock:
            rows = self._db.execute(
                f"""
                SELECT entry_id, url, generation FROM summaries
                 WHERE status = ? AND writeback_status = ?
                 ORDER BY updated_at{limit_clause}
                """,
                parameters,
            ).fetchall()
        return [(row["entry_id"], row["url"], row["generation"]) for row in rows]

    def abandon_pending(self, error: str, *, keep: list[int], retry_at: float | None = None) -> int:
        exclusion = f" AND entry_id NOT IN ({','.join('?' * len(keep))})" if keep else ""
        with self._lock:
            cursor = self._db.execute(
                "UPDATE summaries SET status = ?, error = ?, retry_at = ?, updated_at = ?"
                f" WHERE status = ?{exclusion}",
                (ERROR, error, retry_at, time.time(), PENDING, *keep),
            )
            self._db.commit()
        return cursor.rowcount

    def due_retries(self, now: float, limit: int) -> list[tuple[int, str]]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT entry_id, url FROM summaries
                 WHERE status = ? AND retry_at <= ? ORDER BY retry_at LIMIT ?
                """,
                (ERROR, now, limit),
            ).fetchall()
        return [(row["entry_id"], row["url"]) for row in rows]

    def original_content(self, entry_id: int) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT original FROM summaries WHERE entry_id = ?", (entry_id,)
            ).fetchone()
        return row["original"] if row else None

    def remember_original(
        self, entry_id: int, content: str, *, generation: int | None = None
    ) -> str | None:
        """Store original content once, only while the expected generation is current."""

        condition = "entry_id = ?" + (" AND generation = ?" if generation is not None else "")
        values: tuple[Any, ...] = (entry_id, generation) if generation is not None else (entry_id,)
        with self._lock:
            row = self._db.execute(
                f"SELECT original FROM summaries WHERE {condition}", values
            ).fetchone()
            if row is None:
                return None
            if row["original"] is not None:
                return row["original"]
            cursor = self._db.execute(
                f"UPDATE summaries SET original = ?, updated_at = ? WHERE {condition}",
                (content, time.time(), *values),
            )
            self._db.commit()
        return content if cursor.rowcount == 1 else None
