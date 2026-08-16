"""The small slice of the Miniflux API this service uses."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx

from .config import config


class MinifluxError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def verify_signature(body: bytes, signature: str | None) -> bool:
    """Check a Miniflux webhook's HMAC-SHA256 signature over the raw body."""

    if not config.webhook_secret:
        return False
    expected = hmac.new(config.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    if not signature:
        return False
    try:
        supplied = signature.strip().encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(expected.encode("ascii"), supplied)


def _client() -> httpx.AsyncClient:
    if not config.miniflux_token:
        raise MinifluxError("MINIFLUX_API_TOKEN is not configured.")
    return httpx.AsyncClient(
        base_url=config.miniflux_url.rstrip("/"),
        headers={"X-Auth-Token": config.miniflux_token},
        timeout=httpx.Timeout(30.0),
    )


async def get_entry(entry_id: int) -> dict[str, Any]:
    try:
        async with _client() as client:
            response = await client.get(f"/v1/entries/{entry_id}")
    except httpx.RequestError as exc:
        raise MinifluxError("Miniflux could not be reached.", retryable=True) from exc
    if response.status_code != 200:
        raise MinifluxError(
            f"Miniflux returned HTTP {response.status_code} for entry {entry_id}.",
            retryable=response.status_code in {429, 500, 502, 503, 504},
        )
    try:
        return response.json()
    except ValueError as exc:
        raise MinifluxError("Miniflux returned an invalid entry response.") from exc


async def set_entry_content(entry_id: int, content: str) -> None:
    try:
        async with _client() as client:
            response = await client.put(f"/v1/entries/{entry_id}", json={"content": content})
    except httpx.RequestError as exc:
        raise MinifluxError("Miniflux could not be reached.", retryable=True) from exc
    if response.status_code not in (200, 201, 204):
        raise MinifluxError(
            f"Miniflux refused the update for entry {entry_id} (HTTP {response.status_code}).",
            retryable=response.status_code in {429, 500, 502, 503, 504},
        )
