"""The small slice of the Miniflux API this service uses."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx

from .config import config


class MinifluxError(Exception):
    pass


def verify_signature(body: bytes, signature: str | None) -> bool:
    """Check a Miniflux webhook's HMAC-SHA256 signature over the raw body."""

    if not config.webhook_secret:
        return False
    expected = hmac.new(config.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(signature) and hmac.compare_digest(expected, signature.strip())


def _client() -> httpx.AsyncClient:
    if not config.miniflux_token:
        raise MinifluxError("MINIFLUX_API_TOKEN is not configured.")
    return httpx.AsyncClient(
        base_url=config.miniflux_url.rstrip("/"),
        headers={"X-Auth-Token": config.miniflux_token},
        timeout=httpx.Timeout(30.0),
    )


async def get_entry(entry_id: int) -> dict[str, Any]:
    async with _client() as client:
        response = await client.get(f"/v1/entries/{entry_id}")
    if response.status_code != 200:
        raise MinifluxError(f"Miniflux returned HTTP {response.status_code} for entry {entry_id}.")
    return response.json()


async def set_entry_content(entry_id: int, content: str) -> None:
    async with _client() as client:
        response = await client.put(f"/v1/entries/{entry_id}", json={"content": content})
    if response.status_code not in (200, 201, 204):
        raise MinifluxError(f"Miniflux refused the update for entry {entry_id} (HTTP {response.status_code}).")
