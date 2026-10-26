from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import httpx
import pytest

from reader import app as api
from reader import reader


@dataclass
class FakePage:
    html_content: str
    status: int = 200
    url: str = "https://example.com/article"
    headers: dict[str, str] = field(default_factory=lambda: {"Content-Type": "text/html"})
    history: list = field(default_factory=list)

    @property
    def body(self) -> bytes:
        return self.html_content.encode()


def article_html(marker: str = "normal") -> str:
    return f"<html><body><article>{marker} {'body text ' * 90}</article></body></html>"


@pytest.fixture(autouse=True)
def public_urls(monkeypatch):
    monkeypatch.setenv("READER_ALLOW_DIRECT", "1")

    async def accept(url: str) -> str:
        return url

    monkeypatch.setattr(reader, "validate_url", accept)


def fake_extract(html: str, _url: str):
    if "normal" in html or "rendered" in html or "solved" in html:
        return "A" * 600, {"title": "Test article", "author": "Ada", "sitename": "Example"}
    if "short-good" in html:
        return "S" * 150, {"title": "Short article"}
    return "", {}


@pytest.fixture
def extraction(monkeypatch):
    monkeypatch.setattr(reader, "extract_article", fake_extract)


@pytest.mark.asyncio
async def test_normal_article_uses_http_only(monkeypatch, extraction):
    static = AsyncMock(return_value=FakePage(article_html("normal")))
    dynamic = AsyncMock()
    monkeypatch.setattr(reader, "_static_once", static)
    monkeypatch.setattr(reader, "_dynamic_once", dynamic)

    result = await reader.read_article("https://example.com/article")

    assert result.tier == "http"
    assert result.text == "A" * 600
    dynamic.assert_not_awaited()


@pytest.mark.asyncio
async def test_short_article_with_good_ratio_is_accepted(monkeypatch, extraction):
    html = "<html><body>short-good " + ("x" * 190) + "</body></html>"
    monkeypatch.setattr(reader, "_static_once", AsyncMock(return_value=FakePage(html)))
    dynamic = AsyncMock()
    monkeypatch.setattr(reader, "_dynamic_once", dynamic)

    result = await reader.read_article("https://example.com/short")

    assert result.tier == "http"
    dynamic.assert_not_awaited()


@pytest.mark.asyncio
async def test_js_shell_escalates_to_dynamic(monkeypatch, extraction):
    static = FakePage("<html><body>Loading...</body></html>")
    rendered = FakePage(article_html("rendered"), url="https://example.com/final")
    monkeypatch.setattr(reader, "_static_once", AsyncMock(return_value=static))
    dynamic = AsyncMock(return_value=rendered)
    monkeypatch.setattr(reader, "_dynamic_once", dynamic)

    result = await reader.read_article("https://example.com/app")

    assert result.tier == "dynamic"
    assert result.final_url == "https://example.com/final"
    dynamic.assert_awaited_once()


@pytest.mark.asyncio
async def test_recognized_challenge_uses_stealth(monkeypatch, extraction):
    challenge = FakePage(
        '<html><script src="/cdn-cgi/challenge-platform/start.js"></script></html>',
        status=403,
    )
    solved = FakePage(article_html("solved"))
    monkeypatch.setattr(reader, "_static_once", AsyncMock(return_value=challenge))
    stealth = AsyncMock(return_value=solved)
    dynamic = AsyncMock()
    monkeypatch.setattr(reader, "_stealth_once", stealth)
    monkeypatch.setattr(reader, "_dynamic_once", dynamic)

    result = await reader.read_article("https://example.com/challenged")

    assert result.tier == "stealth"
    stealth.assert_awaited_once()
    assert stealth.await_args.kwargs["solve_cloudflare"] is True
    dynamic.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_cloudflare_challenge_skips_cloudflare_solver(monkeypatch, extraction):
    challenge = FakePage('<html><div class="g-recaptcha"></div></html>', status=403)
    solved = FakePage(article_html("solved"))
    monkeypatch.setattr(reader, "_static_once", AsyncMock(return_value=challenge))
    stealth = AsyncMock(return_value=solved)
    monkeypatch.setattr(reader, "_stealth_once", stealth)

    result = await reader.read_article("https://example.com/challenged")

    assert result.tier == "stealth"
    assert stealth.await_args.kwargs["solve_cloudflare"] is False


@pytest.mark.asyncio
async def test_plain_404_fails_without_browser(monkeypatch, extraction):
    monkeypatch.setattr(
        reader, "_static_once", AsyncMock(return_value=FakePage("<html></html>", status=404))
    )
    stealth = AsyncMock()
    dynamic = AsyncMock()
    monkeypatch.setattr(reader, "_stealth_once", stealth)
    monkeypatch.setattr(reader, "_dynamic_once", dynamic)

    with pytest.raises(reader.ReaderError) as caught:
        await reader.read_article("https://example.com/missing")

    assert caught.value.status_code == 404
    stealth.assert_not_awaited()
    dynamic.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_403_is_not_assumed_to_be_a_challenge(monkeypatch, extraction):
    page = FakePage("<html><body>Access denied.</body></html>", status=403)
    monkeypatch.setattr(reader, "_static_once", AsyncMock(return_value=page))
    stealth = AsyncMock()
    monkeypatch.setattr(reader, "_stealth_once", stealth)

    with pytest.raises(reader.ReaderError) as caught:
        await reader.read_article("https://example.com/private")

    assert caught.value.status_code == 403
    stealth.assert_not_awaited()


@pytest.mark.asyncio
async def test_429_preserves_retry_after_and_never_uses_stealth(monkeypatch, extraction):
    page = FakePage(
        '<html><script src="/cdn-cgi/challenge-platform/start.js"></script></html>',
        status=429,
        headers={"Content-Type": "text/html", "Retry-After": "30"},
    )
    monkeypatch.setattr(reader, "_static_once", AsyncMock(return_value=page))
    stealth = AsyncMock()
    monkeypatch.setattr(reader, "_stealth_once", stealth)

    with pytest.raises(reader.ReaderError) as caught:
        await reader.read_article("https://example.com/busy")

    assert caught.value.status_code == 429
    assert caught.value.retry_after == "30"
    stealth.assert_not_awaited()


@pytest.mark.asyncio
async def test_challenge_served_as_429_without_retry_after_uses_stealth(monkeypatch, extraction):
    challenge = FakePage(
        "<html><body>Vercel Security Checkpoint</body></html>",
        status=429,
        headers={"Content-Type": "text/html", "X-Vercel-Mitigated": "challenge"},
    )
    monkeypatch.setattr(reader, "_static_once", AsyncMock(return_value=challenge))
    stealth = AsyncMock(return_value=FakePage(article_html("solved")))
    monkeypatch.setattr(reader, "_stealth_once", stealth)

    result = await reader.read_article("https://example.com/checkpoint")

    assert result.tier == "stealth"
    assert stealth.await_args.kwargs["solve_cloudflare"] is False
    # The interstitial reloads itself only after its proof-of-work completes.
    assert stealth.await_args.kwargs["settle_ms"] == reader.CHALLENGE_SETTLE_MS


@pytest.mark.asyncio
async def test_plain_429_without_retry_after_is_still_not_a_challenge(monkeypatch, extraction):
    page = FakePage("<html><body>Too many requests.</body></html>", status=429)
    monkeypatch.setattr(reader, "_static_once", AsyncMock(return_value=page))
    stealth = AsyncMock()
    monkeypatch.setattr(reader, "_stealth_once", stealth)

    with pytest.raises(reader.ReaderError) as caught:
        await reader.read_article("https://example.com/busy")

    assert caught.value.code == "rate_limited"
    stealth.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_retries_once_then_returns_gateway_timeout(monkeypatch):
    static = AsyncMock(side_effect=asyncio.TimeoutError)
    monkeypatch.setattr(reader, "_static_once", static)

    with pytest.raises(reader.ReaderError) as caught:
        await reader.read_article("https://example.com/slow")

    assert caught.value.status_code == 504
    assert static.await_count == 2


@pytest.mark.asyncio
async def test_non_html_is_unsupported_without_dynamic(monkeypatch, extraction):
    page = FakePage("%PDF", headers={"Content-Type": "application/pdf"})
    monkeypatch.setattr(reader, "_static_once", AsyncMock(return_value=page))
    dynamic = AsyncMock()
    monkeypatch.setattr(reader, "_dynamic_once", dynamic)

    with pytest.raises(reader.ReaderError) as caught:
        await reader.read_article("https://example.com/file.pdf")

    assert caught.value.status_code == 415
    dynamic.assert_not_awaited()


@pytest.mark.asyncio
async def test_5xx_retries_same_tier(monkeypatch, extraction):
    static = AsyncMock(
        side_effect=[FakePage("<html></html>", status=503), FakePage(article_html("normal"))]
    )
    monkeypatch.setattr(reader, "_static_once", static)

    result = await reader.read_article("https://example.com/recovered")

    assert result.tier == "http"
    assert static.await_count == 2


@pytest.mark.asyncio
async def test_repeated_5xx_does_not_escalate_to_stealth(monkeypatch, extraction):
    challenged_503 = FakePage(
        '<html><script src="/cdn-cgi/challenge-platform/start.js"></script></html>',
        status=503,
    )
    static = AsyncMock(return_value=challenged_503)
    stealth = AsyncMock()
    monkeypatch.setattr(reader, "_static_once", static)
    monkeypatch.setattr(reader, "_stealth_once", stealth)

    with pytest.raises(reader.ReaderError) as caught:
        await reader.read_article("https://example.com/unavailable")

    assert caught.value.status_code == 502
    assert static.await_count == 2
    stealth.assert_not_awaited()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/x",
        "http://127.0.0.1/x",
        "http://169.254.169.254/latest/meta-data",
    ],
)
@pytest.mark.asyncio
async def test_url_safeguards_reject_non_public_targets(monkeypatch, url):
    monkeypatch.undo()
    with pytest.raises(reader.ReaderError) as caught:
        await reader.validate_url(url)
    assert caught.value.status_code == 400


@pytest.mark.asyncio
async def test_unresolvable_name_defers_to_the_proxy(monkeypatch):
    def unresolvable(*args, **kwargs):
        raise reader.socket.gaierror("no resolver")

    monkeypatch.undo()
    monkeypatch.setattr(reader.socket, "getaddrinfo", unresolvable)
    monkeypatch.setenv("READER_OUTBOUND_PROXY", "http://proxy.test:3128")
    assert await reader.validate_url("https://Example.com/a?b=1#c") == "https://Example.com/a?b=1"

    monkeypatch.delenv("READER_OUTBOUND_PROXY")
    with pytest.raises(reader.ReaderError) as caught:
        await reader.validate_url("https://example.com/")
    assert caught.value.code == "dns_error"

    monkeypatch.setenv("READER_OUTBOUND_PROXY", "http://proxy.test:3128")
    for blocked in ("http://localhost/x", "http://10.0.0.1/x"):
        with pytest.raises(reader.ReaderError):
            await reader.validate_url(blocked)


@pytest.mark.asyncio
async def test_final_redirect_target_is_validated(monkeypatch):
    monkeypatch.undo()
    page = FakePage("<html></html>", url="http://169.254.169.254/latest/meta-data")

    with pytest.raises(reader.ReaderError) as caught:
        await reader._validate_response_urls(page)

    assert caught.value.code == "blocked_url"


def ld_json(payload: str) -> str:
    return f'<html><head><script type="application/ld+json">{payload}</script></head></html>'


def test_video_object_identifies_the_page_as_a_video():
    html = ld_json(
        '{"@type": "VideoObject", "embedUrl": "https://www.youtube.com/embed/iNyUmbmQQZg"}'
    )
    assert reader.find_primary_video(html) == "https://www.youtube.com/watch?v=iNyUmbmQQZg"


def test_video_object_is_found_inside_a_graph():
    html = ld_json(
        '{"@graph": [{"@type": "WebPage"},'
        ' {"@type": ["VideoObject"], "contentUrl": "https://youtu.be/iNyUmbmQQZg"}]}'
    )
    assert reader.find_primary_video(html) == "https://www.youtube.com/watch?v=iNyUmbmQQZg"


def test_an_illustrative_embed_is_not_a_video_page():
    # An essay that merely embeds a clip must keep being read as an article.
    html = (
        "<html><body><article>Essay text</article>"
        '<iframe src="https://www.youtube.com/embed/iNyUmbmQQZg"></iframe></body></html>'
    )
    assert reader.find_primary_video(html) is None


def test_non_youtube_and_malformed_video_metadata_are_ignored():
    assert (
        reader.find_primary_video(
            ld_json('{"@type": "VideoObject", "embedUrl": "https://vimeo.com/123"}')
        )
        is None
    )
    assert reader.find_primary_video(ld_json("{not json at all}")) is None
    assert reader.find_primary_video("<html><body>no metadata</body></html>") is None


def test_a_non_youtube_main_video_is_named_by_its_host():
    vimeo = ld_json('{"@type": "VideoObject", "embedUrl": "https://player.vimeo.com/video/123"}')
    youtube = ld_json(
        '{"@type": "VideoObject", "embedUrl": "https://www.youtube.com/embed/iNyUmbmQQZg"}'
    )
    assert reader.find_unsupported_video_host(vimeo) == "vimeo.com"
    assert reader.find_unsupported_video_host(youtube) is None
    assert reader.find_unsupported_video_host(vimeo + youtube) is None
    assert reader.find_unsupported_video_host("<html><body>no metadata</body></html>") is None


@pytest.mark.asyncio
async def test_video_url_is_reported_on_the_result(monkeypatch, extraction):
    page = FakePage(
        article_html("normal")
        + ld_json(
            '{"@type": "VideoObject", "embedUrl": "https://www.youtube.com/embed/iNyUmbmQQZg"}'
        )
    )
    monkeypatch.setattr(reader, "_static_once", AsyncMock(return_value=page))

    result = await reader.read_article("https://example.com/videos/a")

    assert result.video_url == "https://www.youtube.com/watch?v=iNyUmbmQQZg"
    assert result.as_dict()["video_url"] == "https://www.youtube.com/watch?v=iNyUmbmQQZg"
    assert result.video_host is None


def test_zero_body_never_divides_or_accepts_short_extraction():
    assert reader.extraction_is_useful("x" * reader.SHORT_ARTICLE_FLOOR, 0) is False


@pytest.mark.asyncio
async def test_api_routes(monkeypatch):
    result = reader.ArticleResult(
        url="https://example.com/a",
        final_url="https://example.com/a",
        title="Title",
        author=None,
        date=None,
        sitename="Example",
        text="# Article\n\nText",
        video_url=None,
        tier="http",
        status=200,
        elapsed_ms=12,
    )
    mocked = AsyncMock(return_value=result)
    monkeypatch.setattr(api, "read_article", mocked)
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/health")).json() == {"ok": True}
        markdown = await client.get("/v1/read", params={"url": result.url})
        assert markdown.status_code == 200
        assert markdown.text == result.text
        assert markdown.headers["content-type"].startswith("text/markdown")
        payload = (await client.get("/v1/read", params={"url": result.url, "json": "1"})).json()
        assert payload["tier"] == "http"
        assert payload["text"] == result.text
        compatible = await client.get("/https://example.com/a")
        assert compatible.status_code == 200
        assert compatible.text == result.text

        compatible_query = await client.get("/https://example.com/item?id=1&next=a%2Fb")
        assert compatible_query.status_code == 200
        assert mocked.await_args.args[0] == "https://example.com/item?id=1&next=a%2Fb"


@pytest.mark.asyncio
async def test_api_rate_limit_error_includes_retry_after(monkeypatch):
    mocked = AsyncMock(
        side_effect=reader.ReaderError(429, "rate_limited", "Slow down.", retry_after="60")
    )
    monkeypatch.setattr(api, "read_article", mocked)
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/read", params={"url": "https://example.com"})

    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert response.json()["code"] == "rate_limited"


@pytest.mark.asyncio
async def test_scrapling_calls_use_one_internal_attempt(monkeypatch):
    monkeypatch.setenv("READER_OUTBOUND_PROXY", "http://proxy.test:3128")
    page = FakePage("<html></html>")
    static = AsyncMock(return_value=page)
    dynamic = AsyncMock(return_value=page)
    stealth = AsyncMock(return_value=page)
    monkeypatch.setattr(reader.AsyncFetcher, "get", static)
    monkeypatch.setattr(reader.DynamicFetcher, "async_fetch", dynamic)
    monkeypatch.setattr(reader.StealthyFetcher, "async_fetch", stealth)

    await reader._static_once(page.url)
    await reader._dynamic_once(page.url)
    await reader._stealth_once(page.url, solve_cloudflare=True)

    assert static.await_args.kwargs["retries"] == 1
    assert static.await_args.kwargs["follow_redirects"] is False
    assert static.await_args.kwargs["proxy"] == "http://proxy.test:3128"
    assert dynamic.await_args.kwargs["retries"] == 1
    assert dynamic.await_args.kwargs["proxy"] == "http://proxy.test:3128"
    assert "--disable-quic" in dynamic.await_args.kwargs["extra_flags"]
    assert (
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"
        in dynamic.await_args.kwargs["extra_flags"]
    )
    assert stealth.await_args.kwargs["retries"] == 1
    assert stealth.await_args.kwargs["proxy"] == "http://proxy.test:3128"
    assert stealth.await_args.kwargs["solve_cloudflare"] is True
    assert stealth.await_args.kwargs["block_webrtc"] is True
    if reader.SYSTEM_CHROME_PATH:
        assert dynamic.await_args.kwargs["real_chrome"] is True
        assert dynamic.await_args.kwargs["executable_path"] == reader.SYSTEM_CHROME_PATH
        assert stealth.await_args.kwargs["real_chrome"] is True


@pytest.mark.asyncio
async def test_reader_fails_closed_without_proxy_or_explicit_direct_mode(monkeypatch):
    monkeypatch.delenv("READER_OUTBOUND_PROXY", raising=False)
    monkeypatch.delenv("READER_ALLOW_DIRECT", raising=False)
    with pytest.raises(reader.ReaderError) as caught:
        await reader._static_once("https://example.com")
    assert caught.value.code == "proxy_required"


@pytest.mark.parametrize("value", ["socks5://proxy:1080", "not a url", "http:///missing"])
def test_invalid_proxy_configuration_is_rejected(monkeypatch, value):
    monkeypatch.setenv("READER_OUTBOUND_PROXY", value)
    with pytest.raises(reader.ReaderError) as caught:
        reader._proxy_options()
    assert caught.value.code == "proxy_configuration"


@pytest.mark.asyncio
async def test_static_redirect_is_validated_before_following(monkeypatch):
    first = FakePage(
        "<html></html>",
        status=302,
        url="https://example.com/start",
        headers={"Location": "http://127.0.0.1/private"},
    )
    static = AsyncMock(return_value=first)
    monkeypatch.setattr(reader.AsyncFetcher, "get", static)

    async def reject_private(url: str) -> str:
        if url.startswith("http://127.0.0.1"):
            raise reader.ReaderError(400, "blocked_url", "blocked")
        return url

    monkeypatch.setattr(reader, "validate_url", reject_private)

    with pytest.raises(reader.ReaderError) as caught:
        await reader.read_article(first.url)

    assert caught.value.code == "blocked_url"
    static.assert_awaited_once()


@pytest.mark.asyncio
async def test_static_redirect_follows_valid_relative_location(monkeypatch):
    redirect = FakePage(
        "<html></html>",
        status=301,
        url="https://example.com/old/article",
        headers={"Location": "../new/article?part=2"},
    )
    final = FakePage(article_html(), url="https://example.com/new/article?part=2")
    static = AsyncMock(side_effect=[redirect, final])
    monkeypatch.setattr(reader.AsyncFetcher, "get", static)

    page = await reader._static_once(redirect.url)

    assert page is final
    assert static.await_args_list[1].args[0] == "https://example.com/new/article?part=2"
    assert all(call.kwargs["follow_redirects"] is False for call in static.await_args_list)
