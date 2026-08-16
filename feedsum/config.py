"""Configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _keys(name: str) -> tuple[str, ...]:
    """Read one or more credentials, comma- or whitespace-separated."""

    raw = os.environ.get(name, "") or ""
    seen: dict[str, None] = {}
    for part in raw.replace(",", " ").split():
        seen.setdefault(part.strip(), None)
    return tuple(seen)


def _seconds(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    """Read a comma- or whitespace-separated list of delays, in seconds."""

    raw = os.environ.get(name)
    if raw is None:
        return default
    parts = raw.replace(",", " ").split()
    try:
        return tuple(max(0, int(part)) for part in parts)
    except ValueError:
        return default


def _ids(name: str) -> frozenset[int]:
    raw = os.environ.get(name, "") or ""
    return frozenset(int(part) for part in raw.replace(",", " ").split() if part.strip("-").isdigit())


@dataclass(slots=True, frozen=True)
class Config:
    # Article extraction, served by the reader in this repo or by anything that
    # answers the same GET /v1/read contract.
    reader_url: str = field(default_factory=lambda: os.environ.get("FEEDSUM_READER_URL", "http://127.0.0.1:8010"))

    # YouTube: Gemini reads the video itself, so no transcript step is needed.
    # Optional — without a key, video entries fail and articles are unaffected.
    # Several keys may be listed; each has its own quota, so exhausting one only
    # moves work to the next.
    gemini_keys: tuple[str, ...] = field(default_factory=lambda: _keys("GEMINI_API_KEY"))
    gemini_model: str = field(default_factory=lambda: os.environ.get("FEEDSUM_GEMINI_MODEL", "gemini-3.7-flash"))

    # Articles: any OpenAI-compatible endpoint — a provider, or a local runtime.
    openai_base: str = field(default_factory=lambda: os.environ.get("OPENAI_API_BASE", ""))
    openai_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    text_model: str = field(default_factory=lambda: os.environ.get("FEEDSUM_TEXT_MODEL", ""))

    # Miniflux, for reading entry URLs and writing summaries back into entries.
    miniflux_url: str = field(default_factory=lambda: os.environ.get("MINIFLUX_URL", "http://127.0.0.1:8080"))
    miniflux_token: str = field(default_factory=lambda: os.environ.get("MINIFLUX_API_TOKEN", ""))
    webhook_secret: str = field(default_factory=lambda: os.environ.get("FEEDSUM_WEBHOOK_SECRET", ""))

    db_path: str = field(default_factory=lambda: os.environ.get("FEEDSUM_DB", "/var/lib/feedsum/feedsum.db"))

    # Write the summary into the entry so the list itself is the digest.
    write_back: bool = field(default_factory=lambda: os.environ.get("FEEDSUM_WRITE_BACK", "1") != "0")
    # Only these feeds are summarized automatically; empty means every feed.
    feed_allowlist: frozenset[int] = field(default_factory=lambda: _ids("FEEDSUM_FEED_IDS"))

    # One at a time keeps well inside the Gemini free tier's per-minute limits.
    concurrency: int = field(default_factory=lambda: _int("FEEDSUM_CONCURRENCY", 1))
    # Long enough for Gemini to watch an hour-long video.
    job_timeout: int = field(default_factory=lambda: _int("FEEDSUM_JOB_TIMEOUT", 600))
    max_article_chars: int = field(default_factory=lambda: _int("FEEDSUM_MAX_ARTICLE_CHARS", 200_000))

    # How long to wait before each further attempt at an entry that failed for a
    # reason unrelated to the entry itself. Spread wide, because the common cause
    # is an exhausted daily quota. Empty disables retrying.
    retry_delays: tuple[int, ...] = field(
        default_factory=lambda: _seconds("FEEDSUM_RETRY_DELAYS", (120, 900, 3600))
    )
    # A restart interrupts whatever was in flight. Those entries are picked up
    # again on the next start, but only this many: the rest are marked failed
    # rather than replayed, so a restart during a large burst cannot stampede
    # the providers. They stay one click away.
    resume_limit: int = field(default_factory=lambda: _int("FEEDSUM_RESUME_LIMIT", 20))

    def require(self) -> None:
        missing = [
            name
            for name, value in (
                ("OPENAI_API_BASE", self.openai_base),
                ("OPENAI_API_KEY", self.openai_key),
                ("FEEDSUM_TEXT_MODEL", self.text_model),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")


config = Config()
