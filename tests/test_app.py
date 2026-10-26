from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from feedsum import app as service
from feedsum import miniflux
from feedsum.store import (
    ERROR,
    PENDING,
    READY,
    WRITEBACK_DISABLED,
    WRITEBACK_FAILED,
    WRITEBACK_SUCCEEDED,
    Store,
)
from feedsum.summarize import Brief

UI_TOKEN = "ui-token-that-is-independent-and-long"
WEBHOOK_SECRET = "c4fdaf87900b72777cbe8e97d17b97a87d28ae078a462ef4ac5f2541fcf00ce6"
VALID_BRIEF = "1. One\n2. Two\n3. Three\n\n---\n\n## Strengths\nStrong.\n\n## Critique\nLimited."


@pytest.fixture
def api_state(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "api.db"))
    queue: asyncio.Queue = asyncio.Queue()
    service.app.state.store = store
    service.app.state.queue = queue
    monkeypatch.setattr(service, "config", replace(service.config, ui_token=UI_TOKEN))
    monkeypatch.setattr(miniflux, "config", replace(miniflux.config, webhook_secret=WEBHOOK_SECRET))
    yield store, queue
    service._entry_locks.clear()
    store.close()


@pytest_asyncio.fixture
async def client(api_state):
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


def ui_headers(token: str = UI_TOKEN) -> dict[str, str]:
    return {service.UI_TOKEN_HEADER: token}


def signed(body: bytes, *, event: str | None = None, secret: str = WEBHOOK_SECRET):
    headers = {"X-Miniflux-Signature": hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}
    if event:
        headers["X-Miniflux-Event-Type"] = event
    return headers


@pytest.mark.asyncio
async def test_health_is_public_but_every_v1_route_requires_the_ui_token(client):
    assert (await client.get("/healthz")).status_code == 200
    probes = [
        ("POST", "/v1/summaries", {"entry_id": 1}),
        ("POST", "/v1/summaries/status", {"entry_ids": [1]}),
        ("GET", "/v1/summaries/1", None),
    ]
    for method, path, body in probes:
        for token in (None, "", "wrong"):
            headers = {} if token is None else ui_headers(token)
            response = await client.request(method, path, json=body, headers=headers)
            assert response.status_code == 401
    preflight = await client.options(
        "/v1/summaries",
        headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "POST"},
    )
    assert preflight.status_code == 405
    assert "access-control-allow-origin" not in preflight.headers


@pytest.mark.asyncio
async def test_create_poll_batch_unknown_and_miniflux_failure(client, api_state, monkeypatch):
    store, queue = api_state
    monkeypatch.setattr(
        service.miniflux, "get_entry", AsyncMock(return_value={"url": "https://example.com/a"})
    )
    created = await client.post("/v1/summaries", json={"entry_id": 7}, headers=ui_headers())
    assert created.status_code == 200 and created.json()["status"] == "pending"
    assert queue.get_nowait().generation == 1

    polled = await client.get("/v1/summaries/7", headers=ui_headers())
    assert polled.status_code == 200
    statuses = await client.post(
        "/v1/summaries/status", json={"entry_ids": [7, 8]}, headers=ui_headers()
    )
    assert statuses.json() == {"7": "pending"}
    assert (await client.get("/v1/summaries/8", headers=ui_headers())).status_code == 404

    monkeypatch.setattr(
        service.miniflux, "get_entry", AsyncMock(side_effect=miniflux.MinifluxError("unavailable"))
    )
    failed = await client.post("/v1/summaries", json={"entry_id": 9}, headers=ui_headers())
    assert failed.status_code == 502 and failed.json()["detail"] == "unavailable"
    assert store.get(9) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [{}, {"entry_id": 0}, {"entry_id": -1}, {"entry_id": "1"}, {"entry_id": 1, "extra": True}],
)
async def test_create_rejects_malformed_entry_ids(client, body):
    assert (await client.post("/v1/summaries", json=body, headers=ui_headers())).status_code == 422


@pytest.mark.asyncio
async def test_status_rejects_oversized_and_malformed_payloads(client):
    oversized = await client.post(
        "/v1/summaries/status", json={"entry_ids": list(range(1, 502))}, headers=ui_headers()
    )
    malformed = await client.post(
        "/v1/summaries/status", json={"entry_ids": [0, "2"]}, headers=ui_headers()
    )
    assert oversized.status_code == 422
    assert malformed.status_code == 422


@pytest.mark.asyncio
async def test_webhook_hmac_uses_exact_raw_body_and_needs_no_ui_token(client, monkeypatch):
    # Miniflux documents HMAC-SHA256 over the exact raw request body using this
    # example secret. Whitespace is data, not normalized JSON.
    body = b'{"event_type":"ignored"}'
    assert miniflux.verify_signature(body, signed(body)["X-Miniflux-Signature"])
    assert signed(body)["X-Miniflux-Signature"] == (
        "bb4681631a00add5c8638198b8a11cd4f524ad074cbfcc4d69a4a648111b40d0"
    )
    assert not miniflux.verify_signature(body + b" ", signed(body)["X-Miniflux-Signature"])
    assert not miniflux.verify_signature(body, None)

    valid = await client.post("/webhook", content=body, headers=signed(body))
    changed = await client.post("/webhook", content=body + b" ", headers=signed(body))
    missing = await client.post("/webhook", content=body)
    assert valid.status_code == 200 and valid.json()["queued"] == 0
    assert changed.status_code == 401
    assert missing.status_code == 401
    monkeypatch.setattr(miniflux, "config", replace(miniflux.config, webhook_secret=""))
    assert not miniflux.verify_signature(body, signed(body)["X-Miniflux-Signature"])


@pytest.mark.asyncio
async def test_both_webhook_events_allowlist_and_actual_queue_count(client, api_state, monkeypatch):
    _, queue = api_state
    monkeypatch.setattr(
        service, "config", replace(service.config, ui_token=UI_TOKEN, feed_allowlist=frozenset({3}))
    )
    payload = {
        "event_type": "new_entries",
        "entries": [
            {"id": 1, "url": "https://example.com/1", "feed_id": 3},
            {"id": 2, "url": "https://example.com/2", "feed_id": 4},
        ],
    }
    raw = json.dumps(payload).encode()
    response = await client.post("/webhook", content=raw, headers=signed(raw, event="new_entries"))
    assert response.json() == {"queued": 1}
    assert queue.get_nowait().entry_id == 1

    # Seeing the same automatic entry again collapses, so it is not reported as queued.
    duplicate = await client.post("/webhook", content=raw, headers=signed(raw, event="new_entries"))
    assert duplicate.json() == {"queued": 0}

    # Manual save is explicit and bypasses the automatic feed allowlist.
    manual = {
        "event_type": "save_entry",
        "entry": {"id": 2, "url": "https://example.com/2", "feed_id": 4},
    }
    raw = json.dumps(manual).encode()
    saved = await client.post("/webhook", content=raw, headers=signed(raw, event="save_entry"))
    assert saved.json() == {"queued": 1}
    assert queue.get_nowait().entry_id == 2


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_json_malformed_entries_and_oversized_bodies(client):
    bad_json = b"{"
    assert (
        await client.post("/webhook", content=bad_json, headers=signed(bad_json))
    ).status_code == 400
    malformed = json.dumps({"event_type": "save_entry", "entry": {"id": 0, "url": "x"}}).encode()
    assert (
        await client.post("/webhook", content=malformed, headers=signed(malformed))
    ).status_code == 422
    huge = b"x" * (service.MAX_WEBHOOK_BYTES + 1)
    assert (await client.post("/webhook", content=huge, headers=signed(huge))).status_code == 413


@pytest.mark.asyncio
async def test_summary_then_transient_writeback_retry_does_not_call_model_again(
    api_state, monkeypatch
):
    store, queue = api_state
    monkeypatch.setattr(
        service, "config", replace(service.config, retry_delays=(0,), write_back=True)
    )
    summarize = AsyncMock(return_value=Brief(VALID_BRIEF, "article", "model"))
    monkeypatch.setattr(service, "summarize", summarize)
    monkeypatch.setattr(
        service.miniflux, "get_entry", AsyncMock(return_value={"content": "<p>original</p>"})
    )
    setter = AsyncMock(side_effect=[miniflux.MinifluxError("temporary", retryable=True), None])
    monkeypatch.setattr(service.miniflux, "set_entry_content", setter)

    summary, _ = await service._enqueue(store, queue, 10, "https://example.com/10")
    await service._run_summary(store, queue, queue.get_nowait())
    writeback = queue.get_nowait()
    await service._run_writeback(store, queue, writeback)
    await asyncio.gather(*service._retries)
    await service._run_writeback(store, queue, queue.get_nowait())

    result = store.get(10)
    assert summarize.await_count == 1
    assert setter.await_count == 2
    assert result.writeback_status == WRITEBACK_SUCCEEDED
    assert setter.await_args.args[1].count("<h3>Brief</h3>") == 1
    assert setter.await_args.args[1].endswith("<p>original</p>")
    assert result.generation == summary.generation


@pytest.mark.asyncio
async def test_permanent_and_disabled_writeback_keep_the_summary(api_state, monkeypatch):
    store, queue = api_state
    monkeypatch.setattr(
        service.miniflux, "get_entry", AsyncMock(side_effect=miniflux.MinifluxError("denied"))
    )
    claimed, _ = store.claim(20, "https://example.com/20")
    store.complete_summary(
        20,
        claimed.generation,
        source="article",
        model="m",
        payload={"markdown": VALID_BRIEF},
        summary_html="<p>brief</p>",
        write_back=True,
    )
    await service._run_writeback(
        store, queue, service.WorkItem("writeback", 20, claimed.url, claimed.generation)
    )
    failed = store.get(20)
    assert failed.status == READY and failed.writeback_status == WRITEBACK_FAILED
    assert failed.payload["markdown"] == VALID_BRIEF

    disabled, _ = store.claim(21, "https://example.com/21")
    store.complete_summary(
        21,
        disabled.generation,
        source="article",
        model="m",
        payload={"markdown": VALID_BRIEF},
        summary_html="<p>brief</p>",
        write_back=False,
    )
    assert store.get(21).writeback_status == WRITEBACK_DISABLED


@pytest.mark.asyncio
async def test_force_invalidates_sleeping_retry_and_stale_writeback(api_state, monkeypatch):
    store, queue = api_state
    monkeypatch.setattr(service, "config", replace(service.config, retry_delays=(0,)))
    old, _ = await service._enqueue(store, queue, 30, "https://example.com/old")
    queue.get_nowait()
    old_item = service.WorkItem("summary", 30, old.url, old.generation)
    service._fail_summary(store, queue, old_item, "retry", retryable=True, attempt=1)
    newer, _ = await service._enqueue(store, queue, 30, "https://example.com/new", force=True)
    await asyncio.gather(*service._retries)
    items = [queue.get_nowait(), queue.get_nowait()]
    assert {item.generation for item in items} == {old.generation, newer.generation}
    assert store.bump_attempts(30, old.generation) is None

    setter = AsyncMock()
    monkeypatch.setattr(service.miniflux, "set_entry_content", setter)
    await service._run_writeback(
        store, queue, service.WorkItem("writeback", 30, old.url, old.generation)
    )
    setter.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_failures_are_started_over_until_the_rounds_run_out(api_state, monkeypatch):
    store, queue = api_state
    monkeypatch.setattr(
        service, "config", replace(service.config, retry_delays=(), auto_retry_delays=(100, 200))
    )
    await service._enqueue(store, queue, 40, "https://example.com/40")

    expected_delays = [100, 200, None]
    for round_number, delay in enumerate(expected_delays):
        if round_number:
            await service._enqueue(store, queue, 40, "https://example.com/40", automatic=True)
        item = queue.get_nowait()
        service._fail_summary(store, queue, item, "busy", retryable=True, attempt=1)
        failed = store.get(40)
        assert failed.status == ERROR
        if delay is None:
            assert failed.retry_at is None and "retry_at" not in failed.as_dict()
        else:
            assert delay - 5 < failed.retry_at - time.time() <= delay
            assert failed.as_dict()["retry_at"] == failed.retry_at

    await service._enqueue(store, queue, 41, "https://example.com/41")
    service._fail_summary(store, queue, queue.get_nowait(), "paywall", retryable=False, attempt=1)
    assert store.get(41).retry_at is None


@pytest.mark.asyncio
async def test_sweeper_starts_one_due_entry_at_a_time(api_state, monkeypatch):
    store, queue = api_state
    monkeypatch.setattr(service, "RETRY_SWEEP_SECONDS", 0)
    for entry_id in (50, 51, 52):
        store.claim(entry_id, f"https://example.com/{entry_id}")
    store.finish(50, status=ERROR, error="busy", retry_at=1.0)
    store.finish(51, status=ERROR, error="busy", retry_at=2.0)
    store.finish(52, status=ERROR, error="busy", retry_at=time.time() + 3600)

    sweeper = asyncio.create_task(service._retry_failed(store, queue))
    await asyncio.sleep(0.05)
    first = queue.get_nowait()
    # A worker is now busy with the first: the next must wait even though the
    # queue is empty, or it would hold up whatever that worker queues next.
    monkeypatch.setattr(service, "_busy_workers", 1)
    await asyncio.sleep(0.05)
    assert queue.empty()
    monkeypatch.setattr(service, "_busy_workers", 0)
    await asyncio.sleep(0.05)
    second = queue.get_nowait()
    await asyncio.sleep(0.05)
    sweeper.cancel()

    assert (first.entry_id, second.entry_id) == (50, 51)
    assert queue.empty()
    assert store.get(50).status == PENDING and store.get(50).auto_retries == 1
    assert store.get(52).status == ERROR


@pytest.mark.asyncio
async def test_entries_beyond_the_resume_limit_are_left_to_the_sweeper(api_state, monkeypatch):
    store, queue = api_state
    monkeypatch.setattr(service, "config", replace(service.config, resume_limit=1))
    store.claim(60, "https://example.com/60")
    store.claim(61, "https://example.com/61")

    await service._resume(store, queue)

    assert queue.get_nowait().entry_id == 60
    abandoned = store.get(61)
    assert abandoned.status == ERROR and abandoned.retry_at <= time.time()
