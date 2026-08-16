"""HTTP surface: a Miniflux webhook, and an on-demand endpoint for the reader UI.

Summarizing takes tens of seconds, so nothing here does the work inline. A
request records a pending row and queues a job; callers poll for the result.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import miniflux
from .config import config
from .render import markdown_to_html
from .store import ERROR, PENDING, READY, Store, Summary
from .summarize import SummarizeError, summarize


# uvicorn only configures its own loggers, so without this the service's own
# INFO lines (key selection, retries) are dropped by the root logger.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
# httpx logs every request URL at INFO. Credentials must never reach the journal.
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger("feedsum")

SUMMARY_HEADING = "Brief"


MAX_STATUS_LOOKUP = 500

RESTART_ERROR = "Interrupted by a restart — click Brief to run it again."

# A retry is a task that sleeps and then puts the job back on the queue. Holding
# a reference keeps it from being garbage collected mid-wait, and gives shutdown
# something to cancel.
_retries: set[asyncio.Task] = set()


class SummaryRequest(BaseModel):
    entry_id: int
    force: bool = False


class StatusRequest(BaseModel):
    entry_ids: list[int]


def _build_content(summary_html: str, original: str) -> str:
    heading = f"<h3>{SUMMARY_HEADING}</h3>"
    return f"{heading}{summary_html}<hr>{original}"


async def _write_back(store: Store, entry_id: int, summary_html: str) -> None:
    entry = await miniflux.get_entry(entry_id)
    # Rebuild from the first content we ever saw, so repeats never stack up.
    original = store.remember_original(entry_id, entry.get("content") or "")
    await miniflux.set_entry_content(entry_id, _build_content(summary_html, original))


async def _retry_later(queue: asyncio.Queue, entry_id: int, url: str, delay: int) -> None:
    await asyncio.sleep(delay)
    await queue.put((entry_id, url))


def _fail(
    store: Store,
    queue: asyncio.Queue,
    entry_id: int,
    url: str,
    message: str,
    *,
    retryable: bool,
    attempt: int,
) -> None:
    """Record a failure, or schedule another attempt if one is worth making.

    An entry waiting to be retried stays pending, so the button keeps showing it
    as in progress rather than flicking to red and back. The attempt count lives
    in the database, which is what stops a restart from resetting the ladder and
    retrying forever.
    """

    delays = config.retry_delays
    if retryable and attempt <= len(delays):
        delay = delays[attempt - 1]
        logger.info(
            "entry %s: %s Retrying in %ds (attempt %d of %d).",
            entry_id, message, delay, attempt + 1, len(delays) + 1,
        )
        task = asyncio.create_task(_retry_later(queue, entry_id, url, delay))
        _retries.add(task)
        task.add_done_callback(_retries.discard)
        return

    store.finish(entry_id, status=ERROR, error=message)
    logger.warning("entry %s: %s", entry_id, message)


async def _run_job(store: Store, queue: asyncio.Queue, entry_id: int, url: str) -> None:
    attempt = store.bump_attempts(entry_id)
    try:
        brief = await asyncio.wait_for(summarize(url), timeout=config.job_timeout)
    except SummarizeError as exc:
        _fail(store, queue, entry_id, url, str(exc), retryable=exc.retryable, attempt=attempt)
        return
    except asyncio.TimeoutError:
        _fail(store, queue, entry_id, url, "The summary timed out.", retryable=True, attempt=attempt)
        return
    except Exception as exc:  # Provider clients raise a variety of network errors.
        logger.exception("entry %s failed: %s", entry_id, exc)
        _fail(
            store, queue, entry_id, url,
            "The summary failed unexpectedly.", retryable=True, attempt=attempt,
        )
        return

    summary_html = markdown_to_html(brief.markdown)
    store.finish(
        entry_id,
        status=READY,
        source=brief.source,
        model=brief.model,
        payload=brief.as_payload(),
    )

    if config.write_back:
        try:
            await _write_back(store, entry_id, summary_html)
        except Exception as exc:  # A failed write-back must not lose the summary.
            logger.warning("entry %s: summary kept but not written back: %s", entry_id, exc)


async def _worker(store: Store, queue: asyncio.Queue) -> None:
    while True:
        entry_id, url = await queue.get()
        try:
            await _run_job(store, queue, entry_id, url)
        finally:
            queue.task_done()


async def _resume(store: Store, queue: asyncio.Queue) -> None:
    """Pick up the jobs a restart interrupted.

    Nothing else would: `claim` reads a pending row as work already under way, so
    without this an entry caught mid-flight stays amber forever. Only a bounded
    number are replayed — a restart during a large burst would otherwise queue
    the whole burst again, and the providers notice.
    """

    interrupted = store.pending_entries(config.resume_limit)
    abandoned = store.abandon_pending(RESTART_ERROR, keep=[entry_id for entry_id, _ in interrupted])
    for entry_id, url in interrupted:
        await queue.put((entry_id, url))
    if interrupted or abandoned:
        logger.info("resumed %d interrupted entries, abandoned %d", len(interrupted), abandoned)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.require()
    app.state.store = Store(config.db_path)
    app.state.queue = asyncio.Queue()
    await _resume(app.state.store, app.state.queue)
    app.state.workers = [
        asyncio.create_task(_worker(app.state.store, app.state.queue)) for _ in range(config.concurrency)
    ]
    try:
        yield
    finally:
        for task in (*app.state.workers, *_retries):
            task.cancel()
        await asyncio.gather(*app.state.workers, *_retries, return_exceptions=True)
        _retries.clear()
        app.state.store.close()


app = FastAPI(title="feedsum", lifespan=lifespan)


def _state(request: Request) -> tuple[Store, asyncio.Queue]:
    return request.app.state.store, request.app.state.queue


async def _enqueue(store: Store, queue: asyncio.Queue, entry_id: int, url: str, *, force: bool = False) -> Summary:
    summary, needs_work = store.claim(entry_id, url, force=force)
    if needs_work:
        await queue.put((entry_id, url))
    return summary


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.post("/v1/summaries")
async def create_summary(body: SummaryRequest, request: Request) -> JSONResponse:
    store, queue = _state(request)
    # The URL always comes from Miniflux, never from the caller, so this cannot
    # be pointed at an arbitrary target.
    try:
        entry = await miniflux.get_entry(body.entry_id)
    except miniflux.MinifluxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    url = entry.get("url") or ""
    if not url:
        raise HTTPException(status_code=422, detail="That entry has no link to read.")

    summary = await _enqueue(store, queue, body.entry_id, url, force=body.force)
    return JSONResponse(summary.as_dict())


@app.post("/v1/summaries/status")
async def summary_statuses(body: StatusRequest, request: Request) -> dict[str, str]:
    """Which of these entries already have a brief, for one page of the list."""

    store, _ = _state(request)
    wanted = body.entry_ids[:MAX_STATUS_LOOKUP]
    return {str(entry_id): status for entry_id, status in store.statuses(wanted).items()}


@app.get("/v1/summaries/{entry_id}")
async def read_summary(entry_id: int, request: Request) -> JSONResponse:
    store, _ = _state(request)
    summary = store.get(entry_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="No summary has been requested for that entry.")
    return JSONResponse(summary.as_dict())


@app.post("/webhook")
async def webhook(
    request: Request,
    x_miniflux_signature: str | None = Header(default=None),
    x_miniflux_event_type: str | None = Header(default=None),
) -> dict[str, Any]:
    raw = await request.body()
    if not miniflux.verify_signature(raw, x_miniflux_signature):
        raise HTTPException(status_code=401, detail="Invalid signature.")

    payload = await request.json()
    event = x_miniflux_event_type or payload.get("event_type")
    if event == "new_entries":
        entries = payload.get("entries") or []
    elif event == "save_entry":
        entries = [payload["entry"]] if payload.get("entry") else []
    else:
        return {"queued": 0, "ignored": event}

    store, queue = _state(request)
    queued = 0
    for entry in entries:
        entry_id, url = entry.get("id"), entry.get("url")
        feed_id = entry.get("feed_id") or (entry.get("feed") or {}).get("id")
        if not entry_id or not url:
            continue
        if config.feed_allowlist and feed_id not in config.feed_allowlist:
            continue
        # A manual save is an explicit "do this one", so it re-runs.
        await _enqueue(store, queue, int(entry_id), url, force=event == "save_entry")
        queued += 1
    return {"queued": queued}
