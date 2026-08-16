"""Fetch and extract articles without extending or modifying Scrapling."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import shutil
import socket
import time
from dataclasses import asdict, dataclass
from functools import partial
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import trafilatura
from lxml import html as lxml_html
from scrapling.fetchers import AsyncFetcher, DynamicFetcher, StealthyFetcher


NORMAL_ARTICLE_LENGTH = 500
SHORT_ARTICLE_FLOOR = 120
MIN_EXTRACTED_BODY_RATIO = 0.20

MAX_REDIRECTS = 5
MAX_HTML_BYTES = 5 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 15
BROWSER_TIMEOUT_MS = 20_000
# Interstitials that run a proof-of-work in the page reload themselves once it
# passes. Scrapling returns as soon as navigation settles, which is before that.
CHALLENGE_SETTLE_MS = 6_000
TOTAL_TIMEOUT_SECONDS = 45
BROWSER_CONCURRENCY = 2
# Scrapling's ``retries`` option is the total attempt count; one disables its
# internal retry while still performing the request. This module retries once.
SCRAPLING_ATTEMPTS = 1

SYSTEM_CHROME_PATH = next(
    (
        path
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
        if (path := shutil.which(name))
    ),
    None,
)

BROWSER_SEMAPHORE = asyncio.Semaphore(BROWSER_CONCURRENCY)

_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CLOUDFLARE_MARKERS = (
    "cf-chl-",
    "/cdn-cgi/challenge-platform/",
    "challenges.cloudflare.com/turnstile",
    '<form id="challenge-form"',
    "<title>just a moment...</title>",
    "<title>attention required! | cloudflare</title>",
)
_VERCEL_MARKERS = (
    "vercel security checkpoint",
    "x-vercel-challenge-token",
)
_OTHER_CHALLENGE_MARKERS = (
    'class="g-recaptcha"',
    'class="h-captcha"',
)
_LD_JSON_BLOCK = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_YOUTUBE_ID = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:embed|v)/|youtu\.be/|[?&]v=)([A-Za-z0-9_-]{11})"
)


class _DropNoChallengeError(logging.Filter):
    """Hide Scrapling's false error when stealth already bypassed Cloudflare."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "No Cloudflare challenge found" not in record.getMessage()


logging.getLogger("scrapling").addFilter(_DropNoChallengeError())


class ReaderError(Exception):
    """An expected failure that can be presented safely by the API."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after


@dataclass(slots=True)
class ArticleResult:
    url: str
    final_url: str
    title: str | None
    author: str | None
    date: str | None
    sitename: str | None
    text: str
    video_url: str | None
    tier: str
    status: int
    elapsed_ms: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_public_address(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


async def validate_url(url: str) -> str:
    """Validate a URL and reject targets that resolve outside the public internet."""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ReaderError(400, "invalid_url", "The URL is invalid.") from exc

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ReaderError(400, "invalid_url", "Only absolute HTTP(S) URLs are accepted.")
    if parsed.username is not None or parsed.password is not None:
        raise ReaderError(400, "invalid_url", "URLs containing credentials are not accepted.")

    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").rstrip(".").lower()
    except UnicodeError as exc:
        raise ReaderError(400, "invalid_url", "The URL hostname is invalid.") from exc

    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ReaderError(400, "blocked_url", "Local and private network targets are not allowed.")

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None

    if literal is not None:
        addresses = {str(literal)}
    else:
        try:
            records = await asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                port or (443 if parsed.scheme.lower() == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise ReaderError(502, "dns_error", "The target hostname could not be resolved.") from exc
        addresses = {record[4][0] for record in records}

    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise ReaderError(400, "blocked_url", "Local and private network targets are not allowed.")

    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, parsed.query, ""))


def extract_article(html: str, final_url: str) -> tuple[str, dict[str, Any]]:
    """Extract Markdown and metadata from already-fetched HTML."""

    markdown = trafilatura.extract(
        html,
        url=final_url,
        output_format="markdown",
        include_comments=False,
        # These sites append "Up next" and "Explore more" rails to the article
        # body. Balanced extraction keeps them, which reads as article text and
        # silently poisons anything downstream. Precision drops them and leaves
        # genuine long-form essays byte-for-byte identical.
        favor_precision=True,
    ) or ""
    metadata = trafilatura.extract_metadata(html, default_url=final_url)
    return markdown, metadata.as_dict() if metadata else {}


def _ld_json_nodes(data: Any):
    """Walk a JSON-LD document, including nested `@graph` collections."""

    if isinstance(data, list):
        for item in data:
            yield from _ld_json_nodes(item)
    elif isinstance(data, dict):
        yield data
        yield from _ld_json_nodes(data.get("@graph") or [])


def find_primary_video(html: str) -> str | None:
    """Return the video a page declares as its own main content, if any.

    Only a schema.org ``VideoObject`` counts. A bare ``<iframe>`` is not enough:
    an essay that merely illustrates a point with a clip would otherwise be
    mistaken for a page whose whole content is a video.
    """

    for block in _LD_JSON_BLOCK.findall(html):
        try:
            document = json.loads(block)
        except ValueError:
            continue
        for node in _ld_json_nodes(document):
            node_type = node.get("@type") or ""
            if isinstance(node_type, list):
                node_type = " ".join(str(value) for value in node_type)
            if "VideoObject" not in str(node_type):
                continue
            for key in ("embedUrl", "contentUrl", "url"):
                found = _YOUTUBE_ID.search(str(node.get(key) or ""))
                if found:
                    return f"https://www.youtube.com/watch?v={found.group(1)}"
    return None


def body_text_length(html: str) -> int:
    """Return visible-ish body text length for the extraction quality ratio."""

    if not html.strip():
        return 0
    try:
        root = lxml_html.fromstring(html)
    except (ValueError, lxml_html.etree.ParserError):
        return 0
    for element in root.xpath("//script|//style|//noscript|//template"):
        element.drop_tree()
    body = root.find(".//body")
    container = body if body is not None else root
    return len(" ".join(" ".join(container.itertext()).split()))


def extraction_is_useful(markdown: str, body_length: int) -> bool:
    """Apply the small, configurable quality heuristic from the plan."""

    extracted_length = len(markdown.strip())
    if extracted_length >= NORMAL_ARTICLE_LENGTH:
        return True
    if extracted_length < SHORT_ARTICLE_FLOOR:
        return False
    if body_length == 0:
        return False
    return extracted_length / body_length >= MIN_EXTRACTED_BODY_RATIO


def _header(page: Any, name: str) -> str | None:
    wanted = name.lower()
    for key, value in (getattr(page, "headers", {}) or {}).items():
        if str(key).lower() == wanted:
            return str(value)
    return None


def _is_html(page: Any) -> bool:
    content_type = (_header(page, "content-type") or "").lower()
    if content_type:
        return any(value in content_type for value in _HTML_CONTENT_TYPES)
    beginning = str(getattr(page, "html_content", ""))[:512].lstrip().lower()
    return beginning.startswith(("<!doctype html", "<html", "<head", "<body"))


def _challenge_kind(page: Any) -> str | None:
    """Classify recognized challenge pages for the appropriate stealth options."""

    if (_header(page, "cf-mitigated") or "").lower() == "challenge":
        return "cloudflare"
    if (_header(page, "x-vercel-mitigated") or "").lower() == "challenge":
        return "vercel"
    html = str(getattr(page, "html_content", ""))[:262_144].lower()
    if any(marker in html for marker in _CLOUDFLARE_MARKERS):
        return "cloudflare"
    if any(marker in html for marker in _VERCEL_MARKERS):
        return "vercel"
    if any(marker in html for marker in _OTHER_CHALLENGE_MARKERS):
        return "other"
    return None


def _is_challenge(page: Any) -> bool:
    return _challenge_kind(page) is not None


def _page_size(page: Any) -> int:
    body = getattr(page, "body", b"")
    if isinstance(body, str):
        return len(body.encode("utf-8"))
    if body:
        return len(body)
    return len(str(getattr(page, "html_content", "")).encode("utf-8"))


def _browser_runtime_options() -> dict[str, Any]:
    if not SYSTEM_CHROME_PATH:
        return {}
    return {"real_chrome": True, "executable_path": SYSTEM_CHROME_PATH}


async def _static_once(url: str) -> Any:
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        page = await AsyncFetcher.get(
            current_url,
            timeout=FETCH_TIMEOUT_SECONDS,
            retries=SCRAPLING_ATTEMPTS,
            follow_redirects=False,
        )
        location = _header(page, "location")
        if int(getattr(page, "status", 0)) not in _REDIRECT_STATUSES or not location:
            return page
        current_url = await validate_url(urljoin(str(getattr(page, "url", current_url)), location))

    raise ReaderError(502, "too_many_redirects", "The target redirected too many times.")


async def _dynamic_once(url: str) -> Any:
    async with BROWSER_SEMAPHORE:
        return await DynamicFetcher.async_fetch(
            url,
            timeout=BROWSER_TIMEOUT_MS,
            retries=SCRAPLING_ATTEMPTS,
            disable_resources=True,
            **_browser_runtime_options(),
        )


async def _stealth_once(url: str, *, solve_cloudflare: bool = False, settle_ms: int = 0) -> Any:
    async with BROWSER_SEMAPHORE:
        return await StealthyFetcher.async_fetch(
            url,
            timeout=BROWSER_TIMEOUT_MS,
            retries=SCRAPLING_ATTEMPTS,
            disable_resources=True,
            solve_cloudflare=solve_cloudflare,
            wait=settle_ms,
            **_browser_runtime_options(),
        )


async def _fetch_with_retry(fetch: Callable[[str], Awaitable[Any]], url: str, tier: str) -> Any:
    """Retry a network error or 5xx response once on the same tier."""

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            page = await fetch(url)
        except asyncio.CancelledError:
            raise
        except ReaderError:
            raise
        except Exception as exc:  # Fetcher libraries expose several network exception types.
            last_error = exc
            if attempt == 0:
                continue
            if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                raise ReaderError(504, "timeout", f"The {tier} fetch timed out.") from exc
            raise ReaderError(502, "fetch_error", f"The {tier} fetch failed.") from exc
        if int(getattr(page, "status", 0)) >= 500 and attempt == 0:
            continue
        return page

    raise ReaderError(502, "fetch_error", f"The {tier} fetch failed.") from last_error


async def _validate_response_urls(page: Any) -> None:
    # Browser fetchers follow redirects internally, so for those tiers this is
    # necessarily a post-fetch backstop: by the time it runs the browser has
    # already made the request. Keeping the browser off the local network is
    # what actually prevents a blind SSRF, which is why the reader belongs in
    # its own container with no route to the rest of the deployment.
    for response in [*(getattr(page, "history", []) or []), page]:
        response_url = str(getattr(response, "url", ""))
        await validate_url(response_url)


def _check_response(page: Any) -> None:
    status = int(getattr(page, "status", 0))
    if status == 429:
        raise ReaderError(
            429,
            "rate_limited",
            "The target rate-limited the request.",
            retry_after=_header(page, "retry-after"),
        )
    if status in {401, 403, 404, 410}:
        raise ReaderError(status, "upstream_status", f"The target returned HTTP {status}.")
    if status >= 500:
        raise ReaderError(502, "upstream_error", f"The target returned HTTP {status} after a retry.")
    if status < 200 or status >= 300:
        raise ReaderError(502, "upstream_status", f"The target returned HTTP {status}.")
    if not _is_html(page):
        raise ReaderError(415, "unsupported_content", "The target did not return HTML.")
    if _page_size(page) > MAX_HTML_BYTES:
        raise ReaderError(413, "content_too_large", "The returned HTML is too large.")


async def _extract(page: Any) -> tuple[str, dict[str, Any], int]:
    html = str(page.html_content)
    try:
        markdown, metadata = await asyncio.to_thread(extract_article, html, str(page.url))
        length = await asyncio.to_thread(body_text_length, html)
    except Exception as exc:
        raise ReaderError(422, "extraction_failed", "The returned HTML could not be extracted.") from exc
    return markdown, metadata, length


def _result(
    original_url: str,
    page: Any,
    markdown: str,
    metadata: dict[str, Any],
    tier: str,
    started: float,
) -> ArticleResult:
    return ArticleResult(
        url=original_url,
        final_url=str(page.url),
        title=metadata.get("title"),
        author=metadata.get("author"),
        date=metadata.get("date"),
        sitename=metadata.get("sitename"),
        text=markdown,
        video_url=find_primary_video(str(getattr(page, "html_content", "") or "")),
        tier=tier,
        status=int(page.status),
        elapsed_ms=round((time.perf_counter() - started) * 1000),
    )


async def _read_article(url: str, started: float) -> ArticleResult:
    target_url = await validate_url(url)
    page = await _fetch_with_retry(_static_once, target_url, "HTTP")
    await _validate_response_urls(page)

    # A challenge is a specific detected page, not merely any HTTP 403 response.
    # Some interstitials are served as 429; an explicit Retry-After is the only
    # thing that distinguishes a genuine "slow down" from that, so it still wins.
    status = int(page.status)
    challenged_429 = status == 429 and not _header(page, "retry-after")
    challenge_status = status == 403 or challenged_429 or 200 <= status < 300
    challenge_kind = _challenge_kind(page) if challenge_status and _is_html(page) else None
    if challenge_kind is not None:
        if _page_size(page) > MAX_HTML_BYTES:
            raise ReaderError(413, "content_too_large", "The returned HTML is too large.")
        solve_cloudflare = challenge_kind == "cloudflare"
        stealth_fetch = partial(
            _stealth_once,
            solve_cloudflare=solve_cloudflare,
            # Scrapling's own solver already waits for the Cloudflare case.
            settle_ms=0 if solve_cloudflare else CHALLENGE_SETTLE_MS,
        )
        page = await _fetch_with_retry(stealth_fetch, target_url, "stealth browser")
        await _validate_response_urls(page)
        _check_response(page)
        markdown, metadata, body_length = await _extract(page)
        if extraction_is_useful(markdown, body_length):
            return _result(url, page, markdown, metadata, "stealth", started)
        raise ReaderError(422, "article_not_found", "No useful article was found after the challenge.")

    _check_response(page)
    markdown, metadata, body_length = await _extract(page)
    if extraction_is_useful(markdown, body_length):
        return _result(url, page, markdown, metadata, "http", started)

    page = await _fetch_with_retry(_dynamic_once, target_url, "dynamic browser")
    await _validate_response_urls(page)
    _check_response(page)
    markdown, metadata, body_length = await _extract(page)
    if extraction_is_useful(markdown, body_length):
        return _result(url, page, markdown, metadata, "dynamic", started)
    raise ReaderError(422, "article_not_found", "No useful article was found after rendering JavaScript.")


async def read_article(url: str) -> ArticleResult:
    """Read one URL within the configured total time limit."""

    started = time.perf_counter()
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
            return await _read_article(url, started)
    except ReaderError:
        raise
    except TimeoutError as exc:
        raise ReaderError(504, "timeout", "The article request exceeded its total time limit.") from exc
