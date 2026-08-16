from __future__ import annotations

import json
import sqlite3

from feedsum.store import (
    ERROR,
    PENDING,
    READY,
    WRITEBACK_PENDING,
    Store,
)


def test_exact_legacy_schema_migrates_without_losing_payloads_or_originals(tmp_path):
    path = tmp_path / "legacy.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE summaries (
            entry_id INTEGER PRIMARY KEY,
            url TEXT NOT NULL,
            status TEXT NOT NULL,
            source TEXT,
            payload TEXT,
            error TEXT,
            model TEXT,
            original TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )
    payload = {"markdown": "legacy brief", "title": "Kept"}
    db.execute(
        "INSERT INTO summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            12,
            "https://example.com/12",
            READY,
            "article",
            json.dumps(payload),
            None,
            "model",
            "<p>original</p>",
            1.0,
            2.0,
        ),
    )
    db.commit()
    db.close()

    store = Store(str(path))
    migrated = store.get(12)
    assert migrated.payload == payload
    assert store.original_content(12) == "<p>original</p>"
    assert migrated.generation == 0
    assert migrated.writeback_attempts == 0
    columns = {row[1] for row in store._db.execute("PRAGMA table_info(summaries)")}
    assert {
        "attempts",
        "generation",
        "summary_html",
        "writeback_status",
        "writeback_error",
        "writeback_attempts",
    } <= columns
    store.close()


def test_generations_are_monotonic_and_conditional_updates_reject_stale_work(tmp_path):
    store = Store(str(tmp_path / "state.db"))
    first, queued = store.claim(1, "https://example.com/one")
    assert queued and first.generation == 1
    same, queued = store.claim(1, "https://example.com/one")
    assert not queued and same.generation == 1

    newer, queued = store.claim(1, "https://example.com/two", force=True)
    assert queued and newer.generation == 2
    assert store.bump_attempts(1, first.generation) is None
    assert not store.finish(1, generation=first.generation, status=ERROR, error="stale")
    assert not store.complete_summary(
        1,
        first.generation,
        source="article",
        model="m",
        payload={"markdown": "stale"},
        summary_html="<p>stale</p>",
        write_back=True,
    )
    assert store.get(1).status == PENDING
    store.close()


def test_completed_payload_and_writeback_state_are_persisted_for_restart(tmp_path):
    path = tmp_path / "state.db"
    store = Store(str(path))
    claimed, _ = store.claim(2, "https://example.com/two")
    assert store.complete_summary(
        2,
        claimed.generation,
        source="article",
        model="m",
        payload={"markdown": "1. kept"},
        summary_html="<ol><li>kept</li></ol>",
        write_back=True,
    )
    store.close()

    reopened = Store(str(path))
    recovered = reopened.get(2)
    assert recovered.status == READY
    assert recovered.payload == {"markdown": "1. kept"}
    assert recovered.summary_html == "<ol><li>kept</li></ol>"
    assert recovered.writeback_status == WRITEBACK_PENDING
    assert reopened.bump_writeback_attempts(2, claimed.generation) == 1
    reopened.close()

    reopened = Store(str(path))
    assert reopened.get(2).writeback_attempts == 1
    assert reopened.pending_writebacks(10) == [(2, "https://example.com/two", claimed.generation)]
    reopened.close()
