from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from feedsum import app as app_module
from feedsum import config as config_module
from feedsum import summarize
from feedsum.render import markdown_to_html
from feedsum.store import ERROR, PENDING, READY, Store
from feedsum.summarize import SummarizeError, _reject_if_unusable, youtube_id


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=l7Y1BcdO0cw", "l7Y1BcdO0cw"),
        ("https://youtu.be/atry_X1AdbQ", "atry_X1AdbQ"),
        ("https://m.youtube.com/watch?v=-2auAt0SBLI&t=12s", "-2auAt0SBLI"),
        ("https://www.youtube.com/shorts/l7Y1BcdO0cw", "l7Y1BcdO0cw"),
        ("https://aeon.co/essays/something", None),
        ("https://www.youtube.com/channel/UCl9StMQ79LtEvlrskzjoYbQ", None),
        # A lookalike host must not be treated as YouTube.
        ("https://youtube.com.evil.test/watch?v=l7Y1BcdO0cw", None),
    ],
)
def test_youtube_id_detection(url, expected):
    assert youtube_id(url) == expected


def use_keys(monkeypatch, keys, preferred=0, backoff=()):
    """Config is frozen, so swap in a copy carrying the keys under test."""
    monkeypatch.setattr(summarize, "config", replace(summarize.config, gemini_keys=keys))
    monkeypatch.setattr(summarize, "_preferred_key", preferred)
    monkeypatch.setattr(summarize, "_RETRY_BACKOFF_SECONDS", backoff)


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def json(self) -> dict:
        return {} if self.status_code == 200 else {"error": {"status": "RESOURCE_EXHAUSTED"}}


class FakeClient:
    """Records which key each attempt used and replays scripted statuses."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.keys_tried: list[str] = []

    async def post(self, _endpoint, headers=None, json=None, params=None):
        # The key must arrive as a header; a query string would leak it to logs.
        assert params is None, "the API key must not be put in the query string"
        self.keys_tried.append((headers or {})["x-goog-api-key"])
        return FakeResponse(self.statuses[len(self.keys_tried) - 1])


@pytest.mark.asyncio
async def test_an_exhausted_key_falls_through_to_the_next(monkeypatch):
    use_keys(monkeypatch, ("key-a", "key-b", "key-c"))
    client = FakeClient([429, 503, 200])

    response = await summarize._gemini_generate(client, "iNyUmbmQQZg")

    assert response.status_code == 200
    assert client.keys_tried == ["key-a", "key-b", "key-c"]


@pytest.mark.asyncio
async def test_a_working_key_is_not_abandoned(monkeypatch):
    use_keys(monkeypatch, ("key-a", "key-b"))
    client = FakeClient([200])

    await summarize._gemini_generate(client, "iNyUmbmQQZg")

    assert client.keys_tried == ["key-a"]


@pytest.mark.asyncio
async def test_a_rejected_request_is_not_retried_on_other_keys(monkeypatch):
    # A 400 means the request itself is wrong; another key would fail the same way.
    use_keys(monkeypatch, ("key-a", "key-b"))
    client = FakeClient([400])

    response = await summarize._gemini_generate(client, "iNyUmbmQQZg")

    assert response.status_code == 400
    assert client.keys_tried == ["key-a"]


@pytest.mark.asyncio
async def test_exhausting_every_key_reports_the_last_failure(monkeypatch):
    use_keys(monkeypatch, ("key-a", "key-b"))
    client = FakeClient([429, 429])

    response = await summarize._gemini_generate(client, "iNyUmbmQQZg")

    assert response.status_code == 429
    assert client.keys_tried == ["key-a", "key-b"]
    assert "every configured key" in summarize._gemini_error(response)


@pytest.mark.asyncio
async def test_the_working_key_is_remembered_for_the_next_request(monkeypatch):
    use_keys(monkeypatch, ("key-a", "key-b"))
    await summarize._gemini_generate(FakeClient([429, 200]), "iNyUmbmQQZg")

    # The next request must not spend a round trip on the exhausted key again.
    second = FakeClient([200])
    await summarize._gemini_generate(second, "iNyUmbmQQZg")
    assert second.keys_tried == ["key-b"]


@pytest.mark.asyncio
async def test_the_preferred_key_wraps_back_round_when_it_is_spent(monkeypatch):
    # Starting at the last key, an exhausted one wraps to the first, whose
    # daily quota may since have reset.
    use_keys(monkeypatch, ("key-a", "key-b"), preferred=1)
    client = FakeClient([429, 200])

    await summarize._gemini_generate(client, "iNyUmbmQQZg")

    assert client.keys_tried == ["key-b", "key-a"]


@pytest.mark.asyncio
async def test_a_busy_model_is_retried_after_every_key_was_tried(monkeypatch):
    # Gemini 503s under load and clears seconds later, which rotating keys alone
    # cannot fix; the whole sweep is retried before giving up.
    use_keys(monkeypatch, ("key-a", "key-b"), backoff=(0, 0))
    client = FakeClient([503, 503, 503, 503, 200])

    response = await summarize._gemini_generate(client, "iNyUmbmQQZg")

    assert response.status_code == 200
    assert client.keys_tried == ["key-a", "key-b", "key-a", "key-b", "key-a"]


@pytest.mark.asyncio
async def test_no_configured_key_is_reported_clearly(monkeypatch):
    use_keys(monkeypatch, ())
    assert await summarize._gemini_generate(FakeClient([]), "iNyUmbmQQZg") is None
    assert "No Gemini API key" in summarize._gemini_error(None)


def test_keys_are_parsed_from_one_or_many(monkeypatch):
    monkeypatch.setenv("FEEDSUM_TEST_KEYS", "one, two  three")
    assert config_module._keys("FEEDSUM_TEST_KEYS") == ("one", "two", "three")
    monkeypatch.setenv("FEEDSUM_TEST_KEYS", "solo")
    assert config_module._keys("FEEDSUM_TEST_KEYS") == ("solo",)
    monkeypatch.setenv("FEEDSUM_TEST_KEYS", "dup, dup")
    assert config_module._keys("FEEDSUM_TEST_KEYS") == ("dup",)


def test_a_usable_summary_passes_through_untouched():
    assert _reject_if_unusable("1. A real takeaway") == "1. A real takeaway"


def test_the_model_can_refuse_when_it_was_not_given_the_content():
    with pytest.raises(SummarizeError) as caught:
        _reject_if_unusable("UNUSABLE: only a list of links to other articles.")
    assert "only a list of links to other articles" in str(caught.value)


def test_a_refusal_without_a_reason_still_reads_sensibly():
    with pytest.raises(SummarizeError) as caught:
        _reject_if_unusable("UNUSABLE:")
    assert "did not contain the actual content" in str(caught.value)


def test_render_covers_the_shape_the_prompt_asks_for():
    html = markdown_to_html("1. First **claim**\n2. Second claim\n\n---\n\nA short critique.")
    assert html == (
        "<ol><li>First <strong>claim</strong></li><li>Second claim</li></ol>"
        "<hr><p>A short critique.</p>"
    )


def test_render_escapes_untrusted_text():
    html = markdown_to_html("- <script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_claim_queues_once_then_collapses(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    first, needs_work = store.claim(1, "https://example.com/a")
    assert (first.status, needs_work) == (PENDING, True)

    _, needs_work_again = store.claim(1, "https://example.com/a")
    assert needs_work_again is False

    store.finish(1, status=READY, source="article", payload={"markdown": "text"})
    ready, needs_work_after_ready = store.claim(1, "https://example.com/a")
    assert (ready.status, needs_work_after_ready) == (READY, False)

    _, forced = store.claim(1, "https://example.com/a", force=True)
    assert forced is True
    store.close()


def test_original_content_is_remembered_once(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.claim(7, "https://example.com/a")
    assert store.remember_original(7, "<p>original</p>") == "<p>original</p>"
    # A later pass sees the rewritten entry but must keep building from the first.
    assert store.remember_original(7, "<h3>Brief</h3><hr><p>original</p>") == "<p>original</p>"
    store.close()


@pytest.mark.asyncio
async def test_a_busy_provider_is_reported_as_worth_retrying(monkeypatch):
    use_keys(monkeypatch, ("key-a",), backoff=())

    with pytest.raises(SummarizeError) as caught:
        await summarize._summarize_youtube(FakeClient([429]), "https://y.test/x", "iNyUmbmQQZg")

    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_a_rejected_request_is_not_worth_retrying(monkeypatch):
    # A 400 is about the request, and a missing key is about the configuration.
    # Neither changes on its own, so both must land as a plain failure.
    use_keys(monkeypatch, ("key-a",))
    with pytest.raises(SummarizeError) as rejected:
        await summarize._summarize_youtube(FakeClient([400]), "https://y.test/x", "iNyUmbmQQZg")
    assert rejected.value.retryable is False

    use_keys(monkeypatch, ())
    with pytest.raises(SummarizeError) as unconfigured:
        await summarize._summarize_youtube(FakeClient([]), "https://y.test/x", "iNyUmbmQQZg")
    assert unconfigured.value.retryable is False


def test_attempts_are_counted_and_reset_by_a_fresh_claim(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.claim(3, "https://example.com/a")
    assert store.bump_attempts(3) == 1
    assert store.bump_attempts(3) == 2
    # Asking again from the outside starts the ladder over.
    store.claim(3, "https://example.com/a", force=True)
    assert store.bump_attempts(3) == 1
    store.close()


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried_before_it_is_given_up_on(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "config", replace(app_module.config, retry_delays=(0,)))
    store = Store(str(tmp_path / "t.db"))
    queue: asyncio.Queue = asyncio.Queue()
    store.claim(5, "https://example.com/a")

    app_module._fail(store, queue, 5, "https://example.com/a", "Quota gone.", retryable=True, attempt=1)
    await asyncio.gather(*app_module._retries)

    assert queue.get_nowait() == (5, "https://example.com/a")
    # An entry waiting to be retried is still in progress, not a red button.
    assert store.get(5).status == PENDING

    app_module._fail(store, queue, 5, "https://example.com/a", "Quota gone.", retryable=True, attempt=2)

    assert queue.empty()
    assert (store.get(5).status, store.get(5).error) == (ERROR, "Quota gone.")
    store.close()


@pytest.mark.asyncio
async def test_a_permanent_failure_is_not_retried(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    queue: asyncio.Queue = asyncio.Queue()
    store.claim(6, "https://example.com/a")

    app_module._fail(store, queue, 6, "https://example.com/a", "Paywalled.", retryable=False, attempt=1)

    assert queue.empty()
    assert store.get(6).status == ERROR
    store.close()


@pytest.mark.asyncio
async def test_a_restart_resumes_interrupted_entries_without_stampeding(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "config", replace(app_module.config, resume_limit=1))
    store = Store(str(tmp_path / "t.db"))
    for entry_id in (1, 2, 3):
        store.claim(entry_id, f"https://example.com/{entry_id}")
    store.finish(3, status=READY, payload={"markdown": "done"})

    queue: asyncio.Queue = asyncio.Queue()
    await app_module._resume(store, queue)

    # One pending entry is picked up again; the rest are failed rather than left
    # amber forever, and a finished entry is not touched at all.
    assert queue.qsize() == 1
    statuses = store.statuses([1, 2, 3])
    assert sorted(statuses.values()) == [ERROR, PENDING, READY]
    assert statuses[3] == READY
    store.close()


def test_retry_delays_are_read_as_a_list(monkeypatch):
    monkeypatch.delenv("FEEDSUM_TEST_DELAYS", raising=False)
    assert config_module._seconds("FEEDSUM_TEST_DELAYS", (1, 2)) == (1, 2)
    monkeypatch.setenv("FEEDSUM_TEST_DELAYS", "30, 60  90")
    assert config_module._seconds("FEEDSUM_TEST_DELAYS", ()) == (30, 60, 90)
    # An empty setting is a deliberate "never retry", not a missing one.
    monkeypatch.setenv("FEEDSUM_TEST_DELAYS", "")
    assert config_module._seconds("FEEDSUM_TEST_DELAYS", (1, 2)) == ()
