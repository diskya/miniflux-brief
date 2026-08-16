#!/usr/bin/env python3
"""Safely stage a legacy feedsum SQLite database for Compose.

The default is a read-only plan. A cutover requires --execute plus two explicit
confirmation phrases; this utility never stops Miniflux or removes legacy units.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import time
from pathlib import Path

REQUIRED_ENV = {
    "MINIFLUX_URL",
    "MINIFLUX_API_TOKEN",
    "FEEDSUM_WEBHOOK_SECRET",
    "FEEDSUM_UI_TOKEN",
    "OPENAI_API_BASE",
    "OPENAI_API_KEY",
    "FEEDSUM_TEXT_MODEL",
}
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.@-]+$")
ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(command, text=True, capture_output=capture, check=False)
    if result.returncode:
        detail = result.stderr.strip() if capture else ""
        raise SystemExit(f"Command failed: {command[0]} {detail}".rstrip())
    return result.stdout if capture else ""


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{number}: expected NAME=value")
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip()
    missing = sorted(name for name in REQUIRED_ENV if not values.get(name))
    if missing:
        raise ValueError(f"Missing required settings: {', '.join(missing)}")
    return values


def check_env_permissions(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ValueError(
            f"{path} must not be readable or writable by group/other (mode is {mode:o})"
        )


def database_stats(path: Path) -> dict[str, int]:
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"SQLite integrity check failed: {integrity}")
        columns = {row[1] for row in db.execute("PRAGMA table_info(summaries)")}
        if not {"entry_id", "payload", "original"} <= columns:
            raise ValueError("The source does not contain the expected summaries table")
        row = db.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN payload IS NOT NULL THEN 1 ELSE 0 END),
                   SUM(CASE WHEN original IS NOT NULL THEN 1 ELSE 0 END)
              FROM summaries
            """
        ).fetchone()
    return {"summaries": row[0], "payloads": row[1] or 0, "originals": row[2] or 0}


def backup_database(source: Path, destination: Path) -> dict[str, int]:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite backup: {destination}")
    source_uri = f"file:{source.resolve()}?mode=ro"
    with (
        sqlite3.connect(source_uri, uri=True) as source_db,
        sqlite3.connect(destination) as backup_db,
    ):
        source_db.backup(backup_db)
    destination.chmod(0o600)
    source_stats = database_stats(source)
    backup_stats = database_stats(destination)
    if source_stats != backup_stats:
        raise ValueError(f"Backup count mismatch: source={source_stats}, backup={backup_stats}")
    return backup_stats


def seed_volume(backup: Path, volume: str) -> None:
    if not SAFE_NAME.fullmatch(volume):
        raise ValueError("Unsafe Docker volume name")
    if not SAFE_NAME.fullmatch(backup.name):
        raise ValueError("Unsafe backup filename")
    run(["docker", "volume", "create", volume], capture=True)
    mounts = ["-v", f"{backup.parent.resolve()}:/source:ro", "-v", f"{volume}:/data"]
    probe = subprocess.run(
        ["docker", "run", "--rm", *mounts, "alpine:3.22", "sh", "-c", 'test -z "$(ls -A /data)"'],
        check=False,
    )
    if probe.returncode:
        raise ValueError(f"Docker volume {volume!r} is not empty; refusing to overwrite it")
    run(
        [
            "docker",
            "run",
            "--rm",
            *mounts,
            "alpine:3.22",
            "sh",
            "-c",
            f"install -o 1101 -g 1101 -m 0600 /source/{backup.name} /data/feedsum.db",
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--volume", default="miniflux-brief_feedsum-data")
    parser.add_argument("--legacy-writer-service", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-stop", help="must equal STOP_LEGACY_FEEDSUM")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--confirm-start", help="must equal START_NEW_STACK")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not SAFE_NAME.fullmatch(args.legacy_writer_service):
        raise SystemExit("Unsafe systemd service name")

    print("Plan: verify Compose and secret-file permissions; stop only the named legacy writer;")
    print("      create and verify an online SQLite backup including WAL; seed an empty volume.")
    print(f"Legacy writer: {args.legacy_writer_service}")
    print(f"Backup target: {args.backup}")
    print(f"Compose volume: {args.volume}")

    if shutil.which("docker") is None:
        raise SystemExit("Docker is not installed")
    run(["docker", "compose", "version"], capture=True)
    check_env_permissions(args.env_file)
    env_values = load_env(args.env_file)  # Validate without printing names or values.
    if not args.source_db.is_file():
        raise SystemExit(f"Source database does not exist: {args.source_db}")
    stats = database_stats(args.source_db)
    print(
        "Source integrity is OK: "
        f"summaries={stats['summaries']} payloads={stats['payloads']} "
        f"originals={stats['originals']}"
    )
    if args.backup.exists():
        raise SystemExit(f"Backup target already exists: {args.backup}")
    if not args.execute:
        print("Dry run only. No service, database, volume, or stack was changed.")
        return

    if args.confirm_stop != "STOP_LEGACY_FEEDSUM":
        raise SystemExit("Execution requires --confirm-stop STOP_LEGACY_FEEDSUM")
    if args.start and args.confirm_start != "START_NEW_STACK":
        raise SystemExit("Starting requires --confirm-start START_NEW_STACK")
    run([str(ROOT / "ops" / "reader-firewall.sh"), "--check"])
    run(["systemctl", "stop", args.legacy_writer_service])
    stats = backup_database(args.source_db, args.backup)
    print(
        "Verified backup counts: "
        f"summaries={stats['summaries']} payloads={stats['payloads']} "
        f"originals={stats['originals']}"
    )
    seed_volume(args.backup, args.volume)
    print("Seeded feedsum.db as UID/GID 1101 with mode 0600.")

    if args.start:
        environment = os.environ.copy()
        environment["FEEDSUM_ENV_FILE"] = str(args.env_file.resolve())
        if env_values.get("FEEDSUM_PORT"):
            environment["FEEDSUM_PORT"] = env_values["FEEDSUM_PORT"]
        result = subprocess.run(["docker", "compose", "up", "-d", "--wait"], env=environment)
        if result.returncode:
            raise SystemExit("The new stack did not become healthy; use the runbook rollback steps")
        print("New stack is healthy. Complete every canary in docs/MIGRATION.md before acceptance.")
    else:
        print("Stack not started. Use --start with the separate start confirmation after review.")
    completed = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"Legacy units and backup remain in place. Completed at {completed}.")


if __name__ == "__main__":
    main()
