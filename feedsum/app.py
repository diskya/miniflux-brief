"""Authenticated HTTP API and generation-safe background job runner."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import miniflux
from .config import config
from .render import markdown_to_html
from .store import ERROR, Store, Summary
from .summarize import SummarizeError, summarize

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("feedsum")

SUMMARY_HEADING = "Brief"
MAX_STATUS_LOOKUP = 500
MAX_WEBHOOK_BYTES = 1024 * 1024
MAX_WEBHOOK_ENTRIES = 500
MAX_ENTRY_ID = 2**63 - 1
RESTART_ERROR = "Interrupted by a restart — click Brief to run it again."
WRITEBACK_ERROR = "The brief was saved, but Miniflux write-back failed."
UI_TOKEN_HEADER = "X-Feedsum-UI-Token"
# How often to look for failed entries that are due another round.
RETRY_SWEEP_SECONDS = 60

PositiveEntryID = Annotated[int, Field(strict=True, gt=0, le=MAX_ENTRY_ID)]


class SummaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_id: PositiveEntryID
    force: bool = False


class StatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_ids: list[PositiveEntryID] = Field(max_length=MAX_STATUS_LOOKUP)


class WebhookFeed(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: PositiveEntryID


class WebhookEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: PositiveEntryID
    url: str = Field(min_length=1, max_length=8192)
    feed_id: PositiveEntryID | None = None
    feed: WebhookFeed | None = None

    @field_validator("url")
    @classmethod
    def require_http_url(cls, value: str) -> str:
        try:
            parts = urlsplit(value)
        except ValueError as exc:
            raise ValueError("invalid URL") from exc
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise ValueError("invalid URL")
        return value

    @property
    def effective_feed_id(self) -> int | None:
        return self.feed_id or (self.feed.id if self.feed else None)


@dataclass(frozen=True, slots=True)
class WorkItem:
    kind: Literal["summary", "writeback"]
    entry_id: int
    url: str
    generation: int


_retries: set[asyncio.Task] = set()
_entry_locks: dict[int, asyncio.Lock] = {}
_busy_workers = 0


def _entry_lock(entry_id: int) -> asyncio.Lock:
    return _entry_locks.setdefault(entry_id, asyncio.Lock())


def _build_content(summary_html: str, original: str) -> str:
    return f"<h3>{SUMMARY_HEADING}</h3>{summary_html}<hr>{original}"


async def _require_ui_token(
    x_feedsum_ui_token: Annotated[str | None, Header(alias=UI_TOKEN_HEADER)] = None,
) -> None:
    supplied = x_feedsum_ui_token or ""
    if not supplied or not hmac.compare_digest(supplied.encode(), config.ui_token.encode()):
        raise HTTPException(status_code=401, detail="Invalid UI token.")


async def _retry_later(queue: asyncio.Queue[WorkItem], item: WorkItem, delay: int) -> None:
    await asyncio.sleep(delay)
    await queue.put(item)


def _schedule_retry(queue: asyncio.Queue[WorkItem], item: WorkItem, delay: int) -> None:
    task = asyncio.create_task(_retry_later(queue, item, delay))
    _retries.add(task)
    task.add_done_callback(_retries.discard)


def _fail_summary(
    store: Store,
    queue: asyncio.Queue[WorkItem],
    item: WorkItem,
    message: str,
    *,
    retryable: bool,
    attempt: int,
) -> None:
    if not store.is_current(item.entry_id, item.generation):
        return
    delays = config.retry_delays
    if retryable and attempt <= len(delays):
        delay = delays[attempt - 1]
        logger.info(
            "entry %s generation %s: retrying summary in %ds (attempt %d of %d)",
            item.entry_id,
            item.generation,
            delay,
            attempt + 1,
            len(delays) + 1,
        )
        _schedule_retry(queue, item, delay)
        return
    retry_at = _next_round(store, item.entry_id) if retryable else None
    if store.finish(
        item.entry_id, generation=item.generation, status=ERROR, error=message, retry_at=retry_at
    ):
        logger.warning(
            "entry %s generation %s: %s%s",
            item.entry_id,
            item.generation,
            message,
            f" (starting over in {round(retry_at - time.time())}s)" if retry_at else "",
        )


def _next_round(store: Store, entry_id: int) -> float | None:
    """When to start over an entry whose every attempt failed for a passing reason."""

    summary = store.get(entry_id)
    rounds = config.auto_retry_delays
    if summary is None or summary.auto_retries >= len(rounds):
        return None
    return time.time() + rounds[summary.auto_retries]


async def _retry_failed(store: Store, queue: asyncio.Queue[WorkItem]) -> None:
    """Start over failed entries once their next round is due.

    One at a time, and only while the workers are idle, so a backlog of old
    failures never holds up a new entry or floods a provider.
    """

    while True:
        await asyncio.sleep(RETRY_SWEEP_SECONDS)
        if _busy_workers or not queue.empty():
            continue
        try:
            for entry_id, url in store.due_retries(time.time(), limit=1):
                logger.info("entry %s: starting a failed summary over", entry_id)
                await _enqueue(store, queue, entry_id, url, automatic=True)
        except Exception:
            logger.exception("automatic retry sweep failed")


async def _run_summary(store: Store, queue: asyncio.Queue[WorkItem], item: WorkItem) -> None:
    attempt = store.bump_attempts(item.entry_id, item.generation)
    if attempt is None:
        return
    try:
        brief = await asyncio.wait_for(summarize(item.url), timeout=config.job_timeout)
    except SummarizeError as exc:
        _fail_summary(store, queue, item, str(exc), retryable=exc.retryable, attempt=attempt)
        return
    except TimeoutError:
        _fail_summary(
            store,
            queue,
            item,
            "The summary timed out.",
            retryable=True,
            attempt=attempt,
        )
        return
    except Exception as exc:
        logger.exception(
            "entry %s generation %s failed unexpectedly: %s",
            item.entry_id,
            item.generation,
            type(exc).__name__,
        )
        _fail_summary(
            store,
            queue,
            item,
            "The summary failed unexpectedly.",
            retryable=True,
            attempt=attempt,
        )
        return

    summary_html = markdown_to_html(brief.markdown)
    saved = store.complete_summary(
        item.entry_id,
        item.generation,
        source=brief.source,
        model=brief.model,
        payload=brief.as_payload(),
        summary_html=summary_html,
        write_back=config.write_back,
    )
    if saved and config.write_back:
        await queue.put(WorkItem("writeback", item.entry_id, item.url, item.generation))


async def _write_back(store: Store, item: WorkItem) -> None:
    """Write once under the entry lock, so a newer claim cannot overtake it."""

    async with _entry_lock(item.entry_id):
        current = store.get(item.entry_id)
        if current is None or current.generation != item.generation or not current.summary_html:
            return
        entry = await miniflux.get_entry(item.entry_id)
        original = store.remember_original(
            item.entry_id, entry.get("content") or "", generation=item.generation
        )
        if original is None or not store.is_current(item.entry_id, item.generation):
            return
        await miniflux.set_entry_content(
            item.entry_id, _build_content(current.summary_html, original)
        )


async def _run_writeback(store: Store, queue: asyncio.Queue[WorkItem], item: WorkItem) -> None:
    attempt = store.bump_writeback_attempts(item.entry_id, item.generation)
    if attempt is None:
        return
    try:
        await _write_back(store, item)
    except miniflux.MinifluxError as exc:
        retryable = exc.retryable
        safe_detail = str(exc)
    except Exception as exc:
        logger.warning(
            "entry %s generation %s write-back raised %s",
            item.entry_id,
            item.generation,
            type(exc).__name__,
        )
        retryable = True
        safe_detail = WRITEBACK_ERROR
    else:
        store.finish_writeback(item.entry_id, item.generation, successful=True)
        return

    if not store.is_current(item.entry_id, item.generation):
        return
    if retryable and attempt <= len(config.retry_delays):
        delay = config.retry_delays[attempt - 1]
        logger.info(
            "entry %s generation %s: retrying write-back in %ds",
            item.entry_id,
            item.generation,
            delay,
        )
        _schedule_retry(queue, item, delay)
        return
    store.finish_writeback(
        item.entry_id,
        item.generation,
        successful=False,
        error=safe_detail or WRITEBACK_ERROR,
    )


async def _worker(store: Store, queue: asyncio.Queue[WorkItem]) -> None:
    global _busy_workers

    while True:
        item = await queue.get()
        _busy_workers += 1
        try:
            if item.kind == "summary":
                await _run_summary(store, queue, item)
            else:
                await _run_writeback(store, queue, item)
        finally:
            _busy_workers -= 1
            queue.task_done()


async def _resume(store: Store, queue: asyncio.Queue[WorkItem]) -> None:
    interrupted = store.pending_entries(config.resume_limit)
    abandoned = store.abandon_pending(
        RESTART_ERROR,
        keep=[entry_id for entry_id, _, _ in interrupted],
        retry_at=time.time(),
    )
    # Write-back does not spend provider quota or rerun the model, so resume all
    # persisted writes. The summary replay cap remains the stampede protection.
    writebacks = store.pending_writebacks()
    for entry_id, url, generation in interrupted:
        await queue.put(WorkItem("summary", entry_id, url, generation))
    for entry_id, url, generation in writebacks:
        await queue.put(WorkItem("writeback", entry_id, url, generation))
    if interrupted or writebacks or abandoned:
        logger.info(
            "resumed %d summaries and %d write-backs; abandoned %d summaries",
            len(interrupted),
            len(writebacks),
            abandoned,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.require()
    app.state.store = Store(config.db_path)
    app.state.queue = asyncio.Queue()
    await _resume(app.state.store, app.state.queue)
    app.state.workers = [
        asyncio.create_task(_worker(app.state.store, app.state.queue))
        for _ in range(config.concurrency)
    ]
    sweeper = asyncio.create_task(_retry_failed(app.state.store, app.state.queue))
    try:
        yield
    finally:
        for task in (*app.state.workers, sweeper, *_retries):
            task.cancel()
        await asyncio.gather(*app.state.workers, sweeper, *_retries, return_exceptions=True)
        _retries.clear()
        _entry_locks.clear()
        app.state.store.close()


app = FastAPI(title="feedsum", lifespan=lifespan)


def _state(request: Request) -> tuple[Store, asyncio.Queue[WorkItem]]:
    return request.app.state.store, request.app.state.queue


async def _enqueue(
    store: Store,
    queue: asyncio.Queue[WorkItem],
    entry_id: int,
    url: str,
    *,
    force: bool = False,
    automatic: bool = False,
) -> tuple[Summary, bool]:
    async with _entry_lock(entry_id):
        summary, needs_work = store.claim(entry_id, url, force=force, automatic=automatic)
        if needs_work:
            await queue.put(WorkItem("summary", entry_id, url, summary.generation))
    return summary, needs_work


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.post("/v1/summaries", dependencies=[Depends(_require_ui_token)])
async def create_summary(body: SummaryRequest, request: Request) -> JSONResponse:
    store, queue = _state(request)
    try:
        entry = await miniflux.get_entry(body.entry_id)
    except miniflux.MinifluxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    url = entry.get("url") or ""
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise HTTPException(status_code=422, detail="That entry has no valid link to read.")
    summary, _ = await _enqueue(store, queue, body.entry_id, url, force=body.force)
    return JSONResponse(summary.as_dict())


@app.post("/v1/summaries/status", dependencies=[Depends(_require_ui_token)])
async def summary_statuses(body: StatusRequest, request: Request) -> dict[str, str]:
    store, _ = _state(request)
    return {str(entry_id): status for entry_id, status in store.statuses(body.entry_ids).items()}


@app.get("/v1/summaries/{entry_id}", dependencies=[Depends(_require_ui_token)])
async def read_summary(
    entry_id: Annotated[int, Path(gt=0, le=MAX_ENTRY_ID)], request: Request
) -> JSONResponse:
    store, _ = _state(request)
    summary = store.get(entry_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="No summary has been requested for that entry.")
    return JSONResponse(summary.as_dict())


def _webhook_entries(payload: Any, event: str | None) -> list[WebhookEntry] | None:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook payload must be an object.")
    try:
        if event == "new_entries":
            raw_entries = payload.get("entries")
            if not isinstance(raw_entries, list):
                raise HTTPException(status_code=422, detail="new_entries requires an entries list.")
            if len(raw_entries) > MAX_WEBHOOK_ENTRIES:
                raise HTTPException(status_code=413, detail="Too many webhook entries.")
            return [WebhookEntry.model_validate(entry) for entry in raw_entries]
        if event == "save_entry":
            if not isinstance(payload.get("entry"), dict):
                raise HTTPException(status_code=422, detail="save_entry requires an entry object.")
            return [WebhookEntry.model_validate(payload["entry"])]
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Malformed webhook entry.") from exc
    return None


@app.post("/webhook")
async def webhook(
    request: Request,
    x_miniflux_signature: Annotated[str | None, Header(alias="X-Miniflux-Signature")] = None,
    x_miniflux_event_type: Annotated[
        str | None, Header(alias="X-Miniflux-Event-Type", max_length=64)
    ] = None,
) -> dict[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Webhook body is too large.")
    raw = await request.body()
    if len(raw) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Webhook body is too large.")
    if not miniflux.verify_signature(raw, x_miniflux_signature):
        raise HTTPException(status_code=401, detail="Invalid signature.")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Webhook body is not valid JSON.") from exc

    body_event = payload.get("event_type") if isinstance(payload, dict) else None
    event = x_miniflux_event_type or body_event
    if event is not None and (not isinstance(event, str) or len(event) > 64):
        raise HTTPException(status_code=422, detail="Malformed webhook event type.")
    entries = _webhook_entries(payload, event)
    if entries is None:
        return {"queued": 0, "ignored": event}

    store, queue = _state(request)
    queued = 0
    for entry in entries:
        if (
            event == "new_entries"
            and config.feed_allowlist
            and entry.effective_feed_id not in config.feed_allowlist
        ):
            continue
        _, needs_work = await _enqueue(
            store,
            queue,
            entry.id,
            entry.url,
            force=event == "save_entry",
        )
        queued += int(needs_work)
    return {"queued": queued}
