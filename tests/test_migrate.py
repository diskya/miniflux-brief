from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("migrate", ROOT / "ops" / "migrate.py")
migrate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migrate)


def make_legacy(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE summaries (
            entry_id INTEGER PRIMARY KEY, url TEXT NOT NULL, status TEXT NOT NULL,
            source TEXT, payload TEXT, error TEXT, model TEXT, original TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        INSERT INTO summaries VALUES
            (1, 'https://example.com/1', 'ready', 'article', '{"markdown":"one"}',
             NULL, 'm', '<p>one</p>', 1, 1),
            (2, 'https://example.com/2', 'error', NULL, NULL,
             'failed', NULL, NULL, 1, 1);
        """
    )
    db.commit()
    db.close()


def test_online_backup_includes_wal_and_preserves_counts(tmp_path):
    source = tmp_path / "legacy.db"
    backup = tmp_path / "backups" / "feedsum.db"
    make_legacy(source)

    stats = migrate.backup_database(source, backup)

    assert stats == {"summaries": 2, "payloads": 1, "originals": 1}
    assert migrate.database_stats(backup) == stats
    assert backup.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        migrate.backup_database(source, backup)


def test_env_validation_does_not_need_or_return_secret_names_only(tmp_path):
    env = tmp_path / ".env"
    env.write_text("\n".join(f"{name}=test-value" for name in sorted(migrate.REQUIRED_ENV)))
    values = migrate.load_env(env)
    assert set(values) == migrate.REQUIRED_ENV
    env.write_text("MINIFLUX_URL=x\n")
    with pytest.raises(ValueError, match="Missing required settings"):
        migrate.load_env(env)
