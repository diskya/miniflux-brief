"""Configuration.

What comes from the environment is what differs between deployments: where your
Miniflux is, which models to call, what to summarize. Everything below that is a
fixed value rather than a setting — they are limits and safety valves, not
decisions worth making per install. They are gathered at the end of `Config`, in
one place, for anyone who disagrees.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _keys(name: str) -> tuple[str, ...]:
    """Read one or more credentials, comma- or whitespace-separated."""

    raw = os.environ.get(name, "") or ""
    seen: dict[str, None] = {}
    for part in raw.replace(",", " ").split():
        seen.setdefault(part.strip(), None)
    return tuple(seen)


def _ids(name: str) -> frozenset[int]:
    raw = os.environ.get(name, "") or ""
    if not raw.strip():
        return frozenset()
    values: set[int] = set()
    for part in raw.replace(",", " ").split():
        if not part.isdigit() or int(part) <= 0:
            raise ValueError(f"{name} must contain only positive integer feed IDs")
        values.add(int(part))
    return frozenset(values)


@dataclass(slots=True, frozen=True)
class Config:
    # Article extraction, served by the reader in this repo or by anything that
    # answers the same GET /v1/read contract.
    reader_url: str = field(
        default_factory=lambda: os.environ.get("FEEDSUM_READER_URL", "http://127.0.0.1:8010")
    )

    # YouTube: Gemini reads the video itself, so no transcript step is needed.
    # Optional — without a key, video entries fail and articles are unaffected.
    # Several keys may be listed; each has its own quota, so exhausting one only
    # moves work to the next.
    gemini_keys: tuple[str, ...] = field(default_factory=lambda: _keys("GEMINI_API_KEY"))
    gemini_model: str = field(
        default_factory=lambda: os.environ.get("FEEDSUM_GEMINI_MODEL", "gemini-3.7-flash")
    )

    # Articles: any OpenAI-compatible endpoint — a provider, or a local runtime.
    openai_base: str = field(default_factory=lambda: os.environ.get("OPENAI_API_BASE", ""))
    openai_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    text_model: str = field(default_factory=lambda: os.environ.get("FEEDSUM_TEXT_MODEL", ""))

    # Miniflux, for reading entry URLs and writing summaries back into entries.
    miniflux_url: str = field(
        default_factory=lambda: os.environ.get("MINIFLUX_URL", "http://127.0.0.1:8080")
    )
    miniflux_token: str = field(default_factory=lambda: os.environ.get("MINIFLUX_API_TOKEN", ""))
    webhook_secret: str = field(
        default_factory=lambda: os.environ.get("FEEDSUM_WEBHOOK_SECRET", "")
    )
    # Independent browser credential for /v1. A Miniflux session cookie is not
    # authentication for a separately proxied upstream.
    ui_token: str = field(default_factory=lambda: os.environ.get("FEEDSUM_UI_TOKEN", ""))

    db_path: str = field(
        default_factory=lambda: os.environ.get("FEEDSUM_DB", "/var/lib/feedsum/feedsum.db")
    )

    # Write the summary into the entry so the list itself is the digest.
    write_back: bool = field(
        default_factory=lambda: os.environ.get("FEEDSUM_WRITE_BACK", "1") != "0"
    )
    # Only these feeds are summarized automatically; empty means every feed.
    feed_allowlist: frozenset[int] = field(default_factory=lambda: _ids("FEEDSUM_FEED_IDS"))

    # --- Fixed values. Not environment variables, because no deployment has a
    # --- reason to differ; edit them here if yours does.

    # One entry at a time keeps well inside free-tier per-minute limits, and
    # briefs arrive before you read the entry either way.
    concurrency: int = 1
    # Long enough for Gemini to watch an hour-long video.
    job_timeout: int = 600
    # About 50k tokens. Longer than any article; short enough to bound a bill.
    max_article_chars: int = 200_000
    # Enough for 3–5 takeaways and two short assessments, while bounding spend
    # and preventing a broken provider from returning an unbounded document.
    max_summary_tokens: int = 1_200
    # How long to wait before each further attempt at an entry that failed for a
    # reason unrelated to the entry itself. Spread wide, because the common cause
    # is an exhausted daily quota. Empty would disable retrying.
    retry_delays: tuple[int, ...] = (120, 900, 3600)
    # A restart interrupts whatever was in flight. Those entries are picked up
    # again on the next start, but only this many: the rest are marked failed
    # rather than replayed, so a restart during a large burst cannot stampede
    # the providers. They stay one click away.
    resume_limit: int = 20

    def require(self) -> None:
        missing = [
            name
            for name, value in (
                ("MINIFLUX_URL", self.miniflux_url),
                ("MINIFLUX_API_TOKEN", self.miniflux_token),
                ("FEEDSUM_WEBHOOK_SECRET", self.webhook_secret),
                ("FEEDSUM_UI_TOKEN", self.ui_token),
                ("OPENAI_API_BASE", self.openai_base),
                ("OPENAI_API_KEY", self.openai_key),
                ("FEEDSUM_TEXT_MODEL", self.text_model),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")


config = Config()
