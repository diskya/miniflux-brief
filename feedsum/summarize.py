"""Turn one feed entry into a short brief.

Two routes, because the useful input differs. Gemini watches a YouTube video
directly, so there is no transcript step. Everything else is extracted to text
by the reader service first and then summarized by Gemini too, or by an
OpenAI-compatible model when one is configured.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import httpx

from .config import config

logger = logging.getLogger("feedsum.summarize")


UNUSABLE_PREFIX = "UNUSABLE:"
_NUMBERED_TAKEAWAY = re.compile(r"^\s*\d+[.)]\s+\S")
_SECTION_HEADING = re.compile(r"^\s*##\s+\S")
_MARKDOWN_RULE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")

TASK = (
    "Produce a compact brief for someone triaging their reading list.\n"
    "1. Three to five key takeaways as a numbered markdown list. Each one sentence, "
    "concrete and specific: name the actual claim, number, or argument rather than "
    "describing that a topic was discussed.\n"
    "2. Then a '---' rule.\n"
    "3. Then a level-2 markdown heading meaning 'Strengths', and one or two sentences "
    "on what the piece genuinely gets right: rigorous logic, well-supported evidence, "
    "a distinction drawn precisely, or a genuinely novel framing. Name the specific "
    "argument that is strong and say why it holds. Do not praise it in general terms.\n"
    "4. Then a level-2 markdown heading meaning 'Critique', and one or two sentences "
    "on weak reasoning, missing evidence, or unstated assumptions. If the piece is "
    "sound, say what it does not cover instead.\n\n"
    "Judge the strengths and the weaknesses independently: a strong piece should not "
    "be given a manufactured flaw, and a weak one should not be given manufactured "
    "praise.\n\n"
    "If what you were given is not actually the piece itself — mostly navigation, "
    "links and blurbs for other articles, a paywall or consent wall, an error page, "
    "or nothing but a headline and a sentence or two — then do not summarize it, and "
    "do not fill the gaps from your own knowledge of the topic. Reply with exactly "
    f"one line and nothing else:\n{UNUSABLE_PREFIX} <one short sentence naming what "
    "you were given instead>\n\n"
    "Write every heading and sentence in the same language as the source material. "
    "Use plain markdown. Do not add a preamble, a title, or any closing remark."
)

_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"})
_SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class SummarizeError(Exception):
    """A failure worth showing to the reader rather than retrying blindly.

    `retryable` marks the ones that are about the moment rather than the entry —
    an exhausted quota, an overloaded model, a site that was briefly unreachable.
    Those are worth another attempt later; a paywalled page or a refusal is not.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _reject_if_unusable(text: str) -> str:
    """Surface the model's own verdict that it was not given the real content."""

    if not text.upper().startswith(UNUSABLE_PREFIX):
        return text
    reason = text[len(UNUSABLE_PREFIX) :].strip().rstrip(".")
    raise SummarizeError(f"No brief — {reason or 'the page did not contain the actual content'}.")


def _validate_brief_shape(text: str) -> str:
    """Reject malformed model output before it reaches storage or Miniflux."""

    cleaned = _reject_if_unusable(text.strip())
    lines = cleaned.splitlines()
    try:
        rule_index = next(index for index, line in enumerate(lines) if _MARKDOWN_RULE.match(line))
    except StopIteration as exc:
        raise SummarizeError(
            "The summarizer returned a malformed brief (missing sections)."
        ) from exc
    takeaways = sum(bool(_NUMBERED_TAKEAWAY.match(line)) for line in lines[:rule_index])
    headings = [
        index
        for index, line in enumerate(lines[rule_index + 1 :], rule_index + 1)
        if _SECTION_HEADING.match(line)
    ]
    if not 3 <= takeaways <= 5 or len(headings) != 2:
        raise SummarizeError("The summarizer returned a malformed brief (unexpected shape).")
    if any(
        not any(
            line.strip()
            for line in lines[
                index + 1 : headings[position + 1] if position + 1 < len(headings) else None
            ]
        )
        for position, index in enumerate(headings)
    ):
        raise SummarizeError("The summarizer returned a malformed brief (empty section).")
    return cleaned


@dataclass(slots=True)
class Brief:
    markdown: str
    source: str
    model: str
    title: str | None = None

    def as_payload(self) -> dict[str, object]:
        return {"markdown": self.markdown, "title": self.title}


def youtube_id(url: str) -> str | None:
    """Return the video id when the URL is a watchable YouTube video."""

    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().lstrip(".")
    if host in _SHORT_HOSTS:
        candidate = parts.path.lstrip("/").split("/")[0]
    elif host in _YOUTUBE_HOSTS:
        path = parts.path.rstrip("/")
        if path in ("/watch", "/watch/"):
            candidate = (parse_qs(parts.query).get("v") or [""])[0]
        elif path.startswith(("/shorts/", "/live/", "/embed/", "/v/")):
            candidate = path.split("/")[2] if len(path.split("/")) > 2 else ""
        else:
            return None
    else:
        return None
    return candidate if _VIDEO_ID.match(candidate) else None


# A key that is out of quota, or a model that is momentarily overloaded, is a
# property of that one key rather than of the request, so another key may work.
# The same statuses from the reader or the text model mean the same thing there:
# nothing about this entry is wrong, so the job is worth repeating later.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Quota lasts a whole day, so the key that worked last is the one to try first.
# Starting from key one every time would burn a doomed round trip per request
# for the rest of the day. Wrapping means an exhausted key is picked up again
# once the keys after it are spent, by which point its quota may have reset.
_preferred_key = 0

# The model is genuinely flaky under load: a 503 often clears within seconds.
# Rotating keys alone cannot fix that, so each sweep over the keys is retried a
# couple of times. Nobody waits on this synchronously — the page polls.
_RETRY_BACKOFF_SECONDS = (5, 15)


async def _gemini_generate(
    client: httpx.AsyncClient, parts: list[dict], *, system: str | None = None
) -> httpx.Response | None:
    """Ask the last key that worked, then the others, retrying while they are busy."""

    global _preferred_key

    keys = config.gemini_keys
    if not keys:
        return None
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{config.gemini_model}:generateContent"
    payload: dict = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "maxOutputTokens": config.max_summary_tokens + config.gemini_thinking_tokens,
            "thinkingConfig": {"thinkingBudget": config.gemini_thinking_tokens},
        },
    }
    if system:
        payload["system_instruction"] = {"parts": [{"text": system}]}

    last: httpx.Response | None = None
    for pause in (0, *_RETRY_BACKOFF_SECONDS):
        if pause:
            logger.info("gemini: every key busy, retrying in %ss", pause)
            await asyncio.sleep(pause)
        start = _preferred_key % len(keys)
        for offset in range(len(keys)):
            index = (start + offset) % len(keys)
            # The key goes in a header, never the query string: httpx and any
            # proxy in front log request URLs, and a logged URL is a leaked key.
            response = await client.post(
                endpoint, headers={"x-goog-api-key": keys[index]}, json=payload
            )
            if response.status_code == 200:
                if index != _preferred_key:
                    logger.info("gemini: now preferring key %d of %d", index + 1, len(keys))
                _preferred_key = index
                return response
            last = response
            if response.status_code not in _RETRYABLE_STATUSES:
                return response
            logger.warning(
                "gemini key %d of %d unavailable (HTTP %s)",
                index + 1,
                len(keys),
                response.status_code,
            )
    return last


async def _gemini_reply(
    client: httpx.AsyncClient, what: str, parts: list[dict], *, system: str | None = None
) -> str:
    """Return Gemini's reply about the video or article, or raise why there is none."""

    response = await _gemini_generate(client, parts, system=system)
    if response is None or response.status_code != 200:
        # No key at all is a configuration mistake and will not fix itself; an
        # exhausted quota or an overloaded model will.
        transient = response is not None and response.status_code in _RETRYABLE_STATUSES
        raise SummarizeError(_gemini_error(response, what), retryable=transient)

    body = response.json()
    candidates = body.get("candidates") or []
    if not candidates:
        # A refusal or a video Gemini could not open arrives with no candidate.
        blocked = (body.get("promptFeedback") or {}).get("blockReason")
        raise SummarizeError(
            f"Gemini returned no summary for the {what} ({blocked or 'no candidates'})."
        )
    reply = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in reply).strip()
    if not text:
        raise SummarizeError(f"Gemini returned an empty summary for the {what}.")
    return text


async def _summarize_youtube(client: httpx.AsyncClient, url: str, video_id: str) -> Brief:
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    text = await _gemini_reply(
        client, "video", [{"file_data": {"file_uri": watch_url}}, {"text": TASK}]
    )
    return Brief(markdown=_validate_brief_shape(text), source="youtube", model=config.gemini_model)


def _gemini_error(response: httpx.Response | None, what: str) -> str:
    if response is None:
        return "No Gemini API key is configured."
    try:
        detail = response.json().get("error", {})
    except ValueError:
        detail = {}
    status = detail.get("status") or response.status_code
    keys = len(config.gemini_keys)
    scope = "every configured key" if keys > 1 else "the configured key"
    if response.status_code == 429:
        return f"Gemini quota exhausted on {scope} for now."
    return f"Gemini rejected the {what} request ({status})."


async def _read_article(
    client: httpx.AsyncClient, url: str
) -> tuple[str, str | None, str | None, str | None]:
    response = await client.get(
        f"{config.reader_url.rstrip('/')}/v1/read",
        params={"url": url, "json": "true"},
        timeout=httpx.Timeout(120.0),
    )
    if response.status_code != 200:
        try:
            code = response.json().get("code", response.status_code)
        except ValueError:
            code = response.status_code
        # The reader reports a failed fetch, a timeout or a rate limit with the
        # matching status, so the entry itself may well read fine later.
        raise SummarizeError(
            f"The article could not be extracted ({code}).",
            retryable=response.status_code in _RETRYABLE_STATUSES,
        )
    body = response.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise SummarizeError("The article extracted to empty text.")
    return (
        text[: config.max_article_chars],
        body.get("title"),
        body.get("video_url"),
        body.get("video_host"),
    )


# What to show instead of an error when the page is a film we cannot watch: the
# page's own description is still more than the feed's one-line teaser.
UNSUPPORTED_VIDEO_EXCERPT_CHARS = 3_000


def _unsupported_video_brief(host: str, article: str) -> str:
    excerpt = article[:UNSUPPORTED_VIDEO_EXCERPT_CHARS].strip()
    note = (
        f"*Contains a video on {host}, which is not supported for parsing. The page's description:*"
    )
    return f"{note}\n\n{excerpt}"


async def _summarize_article(client: httpx.AsyncClient, url: str) -> Brief:
    article, title, video_url, video_host = await _read_article(client, url)

    # A page whose own content is a video: the extracted text is only the blurb
    # and the surrounding rails, so summarizing it would describe the page
    # furniture rather than the piece. Watch the video instead.
    embedded = youtube_id(video_url or "")
    if embedded:
        brief = await _summarize_youtube(client, url, embedded)
        brief.title = title or brief.title
        return brief

    prompt = f"Title: {title or 'Unknown'}\nURL: {url}\n\n{article}"
    if config.openai_base:
        text, model = await _openai_reply(client, prompt), config.text_model
    else:
        text = await _gemini_reply(client, "article", [{"text": prompt}], system=TASK)
        model = config.gemini_model
    # The model still reads the page first, since an essay may also declare a
    # film. Only when it finds nothing but a blurb is the blurb shown as is.
    if video_host and text.upper().startswith(UNUSABLE_PREFIX):
        return Brief(
            markdown=_unsupported_video_brief(video_host, article),
            source="excerpt",
            model=model,
            title=title,
        )
    return Brief(markdown=_validate_brief_shape(text), source="article", model=model, title=title)


async def _openai_reply(client: httpx.AsyncClient, prompt: str) -> str:
    response = await client.post(
        f"{config.openai_base.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {config.openai_key}"},
        json={
            "model": config.text_model,
            "max_tokens": config.max_summary_tokens,
            "messages": [
                {"role": "system", "content": TASK},
                {"role": "user", "content": prompt},
            ],
        },
    )
    if response.status_code != 200:
        raise SummarizeError(
            f"The summarizer rejected the article ({response.status_code}).",
            retryable=response.status_code in _RETRYABLE_STATUSES,
        )
    choices = response.json().get("choices") or []
    text = (((choices[0] if choices else {}).get("message") or {}).get("content") or "").strip()
    if not text:
        raise SummarizeError("The summarizer returned an empty response.")
    return text


async def summarize(url: str) -> Brief:
    """Summarize one URL, choosing the route from the URL itself."""

    video_id = youtube_id(url)
    timeout = httpx.Timeout(float(config.job_timeout))
    async with httpx.AsyncClient(timeout=timeout) as client:
        if video_id:
            return await _summarize_youtube(client, url, video_id)
        return await _summarize_article(client, url)
