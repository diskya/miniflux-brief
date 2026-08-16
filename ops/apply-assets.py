#!/usr/bin/env python3
"""Push ops/custom.js and ops/custom.css into Miniflux's settings.

Entirely optional: both files can be pasted into Settings -> Custom JavaScript
and Settings -> Custom CSS by hand. This exists because editing them and then
re-pasting twice gets old, and because it keeps any CSS you wrote yourself: the
stylesheet is split at the marker line that starts the bundled CSS, and only the
part below it is replaced.

Miniflux's API can set `stylesheet`, but has no field for the custom JavaScript,
so doing both in one step means going to the database directly. Pass whatever
your deployment needs to reach it:

    python3 ops/apply-assets.py --dry-run
    python3 ops/apply-assets.py --psql "sudo -u postgres psql -d miniflux2"
    python3 ops/apply-assets.py --psql "docker compose exec -T db psql -U miniflux -d miniflux"
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
# The first line of ops/custom.css. Everything from here down is ours to replace.
MARKER = "/* feedsum -"


def quote_tag(*bodies: str) -> str:
    """Pick a dollar-quote tag that appears in none of the bodies."""

    for tag in ("$asset$", "$asset1$", "$asset2$", "$minifluxbrief$"):
        if all(tag not in body for body in bodies):
            return tag
    raise SystemExit("Could not find a safe quote tag for these files.")


def run_psql(psql: list[str], sql: str, *, capture: bool) -> str:
    result = subprocess.run(
        [*psql, "-v", "ON_ERROR_STOP=1", *(["-tAc", sql] if capture else [])],
        input=None if capture else sql,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.exit(f"psql failed:\n{result.stderr.strip()}")
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--psql",
        default="sudo -u postgres psql -d miniflux2",
        help="command that opens a psql session on the Miniflux database",
    )
    parser.add_argument("--user-id", type=int, default=1, help="Miniflux user id (default: 1)")
    parser.add_argument("--dry-run", action="store_true", help="print what would change and stop")
    args = parser.parse_args()

    css = (HERE / "custom.css").read_text()
    js = (HERE / "custom.js").read_text()
    if not css.startswith(MARKER):
        sys.exit(f"ops/custom.css no longer starts with {MARKER!r}; update MARKER in this script.")

    psql = shlex.split(args.psql)
    existing = run_psql(
        psql, f"SELECT stylesheet FROM users WHERE id = {args.user_id}", capture=True
    ).rstrip("\n")
    kept = existing.split(MARKER)[0].rstrip()
    stylesheet = f"{kept}\n\n{css}" if kept else css

    if args.dry_run:
        print(f"user {args.user_id}: keeping {len(kept)} bytes of your own CSS, "
              f"writing {len(stylesheet)} bytes of stylesheet and {len(js)} bytes of custom JS")
        return

    tag = quote_tag(stylesheet, js)
    run_psql(
        psql,
        f"UPDATE users SET stylesheet = {tag}{stylesheet}{tag}, custom_js = {tag}{js}{tag}"
        f" WHERE id = {args.user_id};",
        capture=False,
    )
    print(f"Applied to Miniflux user {args.user_id}. Reload the reading page.")


if __name__ == "__main__":
    main()
