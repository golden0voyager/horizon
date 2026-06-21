"""Phase 4 Layer 1 unit tests for ``src.scrapers.twitter``.

Target paths (ALL non-Playwright):
- ``fetch`` early-return when disabled / no users / cookie-dir empty.
- ``_load_browser_cookies`` returns ``[]`` when file missing.
- ``_parse_tweet`` produces a valid ``ContentItem`` or ``None`` on bad input.
- ``TwitterScraper.append_discussion_content`` marker formatting.
- ``fetch_replies_for_item`` short-circuits + httpx.MockTransport happy path
  (start → status poll → dataset items, scored+truncated to max_replies).

No ``requires_playwright`` tests live here; Layer 2 (Playwright real
chromium) tests live in ``tests/test_twitter.py`` (PR 2 / PR 3).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from src.models import ContentItem, TwitterConfig
from src.scrapers.twitter import TwitterScraper, _load_browser_cookies

# ---------------------------------------------------------------------------
# _load_browser_cookies
# ---------------------------------------------------------------------------


def test_load_browser_cookies_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert _load_browser_cookies(str(tmp_path / "missing.json")) == []


def test_load_browser_cookies_converts_to_playwright_format(tmp_path: Path) -> None:
    fixture = [
        {
            "name": "auth_token",
            "value": "abc123",
            "domain": ".x.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "expirationDate": 1750000000.0,
        },
        {
            "name": "ct0",
            "value": "def456",
            "domain": ".x.com",
            "secure": True,
            # optional fields absent
        },
    ]
    path = tmp_path / "cookies.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")

    out = _load_browser_cookies(str(path))
    assert len(out) == 2
    assert out[0]["name"] == "auth_token"
    assert out[0]["value"] == "abc123"
    assert out[0]["domain"] == ".x.com"
    assert "expires" in out[0]
    assert "expires" not in out[1]
    assert out[1]["httpOnly"] is False


# ---------------------------------------------------------------------------
# _parse_tweet
# ---------------------------------------------------------------------------


def _build_tweet_dict(**overrides: Any) -> dict[str, Any]:
    base = {
        "tweet_id": "1737000000000000000",
        "text": "hello world",
        "datetime_raw": "Wed Jan 15 12:00:00 +0000 2026",
        "is_retweet": False,
        "images": [],
    }
    base.update(overrides)
    base["datetime"] = datetime.strptime(base["datetime_raw"], "%a %b %d %H:%M:%S %z %Y").isoformat()
    return base


def _make_scraper() -> TwitterScraper:
    """Construct a bare TwitterScraper for calling _parse_tweet (instance method)."""

    return TwitterScraper(TwitterConfig())


def test_parse_tweet_returns_content_item() -> None:
    tweet = _build_tweet_dict(text="hello world this is a great tweet")
    item = _make_scraper()._parse_tweet(tweet, "alice")
    assert isinstance(item, ContentItem)
    assert "alice" in (item.author or "")
    assert item.title.startswith("@alice: hello world")
    assert item.url.host == "x.com"


def test_parse_tweet_returns_none_when_tweet_id_missing() -> None:
    tweet = _build_tweet_dict()
    tweet["tweet_id"] = ""
    assert _make_scraper()._parse_tweet(tweet, "alice") is None


def test_parse_tweet_returns_none_when_text_empty() -> None:
    tweet = _build_tweet_dict(text="")
    assert _make_scraper()._parse_tweet(tweet, "alice") is None


def test_parse_tweet_returns_none_when_unparseable_datetime() -> None:
    # Production reads ``tweet["datetime"]`` (not ``datetime_raw``). Override that.
    tweet = _build_tweet_dict()
    tweet["datetime"] = "not-an-iso-timestamp"
    assert _make_scraper()._parse_tweet(tweet, "alice") is None


def test_parse_tweet_truncates_long_title_to_50_chars_with_ellipsis() -> None:
    long_text = "x" * 80
    tweet = _build_tweet_dict(text=long_text)
    item = _make_scraper()._parse_tweet(tweet, "alice")
    assert item is not None
    title_body = item.title.replace("@alice: ", "")
    assert title_body.endswith("...")
    assert len(title_body) <= 60


def test_parse_tweet_strips_newlines_in_title() -> None:
    tweet = _build_tweet_dict(text="line1\nline2\nline3 short")
    item = _make_scraper()._parse_tweet(tweet, "alice")
    assert item is not None
    assert "\n" not in item.title


def test_parse_tweet_includes_images_in_metadata() -> None:
    images = [
        "https://pbs.twimg.com/media/aaa.jpg",
        "https://pbs.twimg.com/media/bbb.jpg",
    ]
    tweet = _build_tweet_dict(images=images)
    item = _make_scraper()._parse_tweet(tweet, "alice")
    assert item is not None
    assert item.metadata["images"] == images


# ---------------------------------------------------------------------------
# TwitterScraper.append_discussion_content
# ---------------------------------------------------------------------------


def test_append_discussion_content_returns_false_when_no_replies() -> None:
    item = _build_item_with_content("hello")
    assert TwitterScraper.append_discussion_content(item, []) is False
    assert item.content == "hello"


def test_append_discussion_content_appends_reply_marker() -> None:
    item = _build_item_with_content("hello")
    assert TwitterScraper.append_discussion_content(item, ["[@a | \u2764 5] great take", "[@b | \u2764 1] ok"]) is True
    assert "--- Top Comments ---" in item.content
    assert "[@a | \u2764 5] great take" in item.content
    assert "[@b | \u2764 1] ok" in item.content


def test_append_discussion_content_handles_empty_content() -> None:
    item = ContentItem(
        id="t:t:1",
        source_type="twitter",
        title="t",
        url="https://x.com/u/status/1",
        content=None,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    TwitterScraper.append_discussion_content(item, ["[@a] yes"])
    assert item.content is not None
    assert "--- Top Comments ---" in item.content


def _build_item_with_content(content: str) -> ContentItem:
    return ContentItem(
        id="t:t:1",
        source_type="twitter",
        title="t",
        url="https://x.com/u/status/1",
        content=content,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# fetch (early-return branches)
# ---------------------------------------------------------------------------


def test_fetch_returns_empty_when_config_disabled() -> None:
    cfg = TwitterConfig(enabled=False, users=["alice"])
    scraper = TwitterScraper(cfg)
    out = asyncio.run(scraper.fetch(datetime(2026, 1, 1, tzinfo=UTC)))
    assert out == []


def test_fetch_returns_empty_when_no_users() -> None:
    cfg = TwitterConfig(enabled=True, users=[])
    scraper = TwitterScraper(cfg)
    out = asyncio.run(scraper.fetch(datetime(2026, 1, 1, tzinfo=UTC)))
    assert out == []


def test_fetch_returns_empty_when_no_cookie_files(tmp_path: Path) -> None:
    cfg = TwitterConfig(
        enabled=True,
        users=["alice"],
        cookie_dir=str(tmp_path / "no_cookies"),
        cookie_file_pattern="x_cookies_*.json",
    )
    scraper = TwitterScraper(cfg)
    out = asyncio.run(scraper.fetch(datetime(2026, 1, 1, tzinfo=UTC)))
    assert out == []


# ---------------------------------------------------------------------------
# fetch_replies_for_item — short-circuit semantics + httpx.MockTransport happy path
# ---------------------------------------------------------------------------


def _make_apify_mock_transport(
    *,
    run_id: str = "run-123",
    dataset_id: str = "ds-abc",
    status: str = "SUCCEEDED",
    rows: list[dict[str, Any]] | None = None,
    start_status: int = 200,
    poll_status: int = 200,
    dataset_status: int = 200,
) -> httpx.MockTransport:
    rows = rows or []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/runs" in url:
            return httpx.Response(
                start_status,
                json={"data": {"id": run_id, "defaultDatasetId": dataset_id}},
            )
        if f"/actor-runs/{run_id}" in url:
            return httpx.Response(poll_status, json={"data": {"status": status}})
        if f"/datasets/{dataset_id}/items" in url:
            return httpx.Response(dataset_status, json=rows)
        return httpx.Response(404, json={"error": "unmocked", "url": url})

    return httpx.MockTransport(handler)


def _build_item_with_conversation_id(conv_id: str | None) -> ContentItem:
    metadata: dict[str, Any] = {}
    if conv_id is not None:
        metadata["conversation_id"] = conv_id
    return ContentItem(
        id="t:t:1",
        source_type="twitter",
        title="t",
        url="https://x.com/u/status/1",
        content="body",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        metadata=metadata,
    )


def test_fetch_replies_returns_empty_when_disabled() -> None:
    """``fetch_reply_text=False`` short-circuits before any HTTP call."""
    cfg = TwitterConfig(fetch_reply_text=False, conversation_id="abc")
    client = MagicMock(spec=httpx.AsyncClient)
    scraper = TwitterScraper(cfg, client)
    item = _build_item_with_conversation_id("conv-1")
    out = asyncio.run(scraper.fetch_replies_for_item(item))
    assert out == []


def test_fetch_replies_returns_empty_when_no_client() -> None:
    cfg = TwitterConfig(fetch_reply_text=True, conversation_id="abc")
    scraper = TwitterScraper(cfg, http_client=None)
    item = _build_item_with_conversation_id("conv-1")
    out = asyncio.run(scraper.fetch_replies_for_item(item))
    assert out == []


def test_fetch_replies_returns_empty_when_no_conv_id_or_tweet_id() -> None:
    cfg = TwitterConfig(fetch_reply_text=True)
    client = MagicMock(spec=httpx.AsyncClient)
    scraper = TwitterScraper(cfg, client)
    item = _build_item_with_conversation_id(None)
    out = asyncio.run(scraper.fetch_replies_for_item(item))
    assert out == []


def test_fetch_replies_returns_empty_when_no_apify_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    cfg = TwitterConfig(fetch_reply_text=True)
    client = MagicMock(spec=httpx.AsyncClient)
    scraper = TwitterScraper(cfg, client)
    item = _build_item_with_conversation_id("conv-1")
    out = asyncio.run(scraper.fetch_replies_for_item(item))
    assert out == []


def test_fetch_replies_returns_empty_when_start_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APIFY_TOKEN", "tok")

    cfg = TwitterConfig(fetch_reply_text=True)

    def handler(request: httpx.Request) -> httpx.Response:
        if "/runs" in str(request.url):
            return httpx.Response(403, json={"error": "forbidden"})
        return httpx.Response(200, json={"data": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    scraper = TwitterScraper(cfg, client)
    item = _build_item_with_conversation_id("conv-1")
    out = asyncio.run(scraper.fetch_replies_for_item(item))
    assert out == []


def test_fetch_replies_returns_empty_when_status_not_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APIFY_TOKEN", "tok")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/runs" in url and "/actor-runs/" not in url:
            return httpx.Response(200, json={"data": {"id": "r1", "defaultDatasetId": "d1"}})
        if "/actor-runs/r1" in url:
            return httpx.Response(200, json={"data": {"status": "FAILED"}})
        return httpx.Response(200, json=[])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cfg = TwitterConfig(fetch_reply_text=True)
    scraper = TwitterScraper(cfg, client)
    item = _build_item_with_conversation_id("conv-1")
    out = asyncio.run(scraper.fetch_replies_for_item(item))
    assert out == []


def test_fetch_replies_returns_empty_when_max_replies_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APIFY_TOKEN", "tok")
    rows = [
        {"user": {"handle": "alice"}, "favorite_count": 5, "reply_count": 0, "text": "hi"},
    ]
    transport = _make_apify_mock_transport(rows=rows)
    client = httpx.AsyncClient(transport=transport)
    cfg = TwitterConfig(fetch_reply_text=True, max_replies_per_tweet=0, reply_min_likes=0)
    scraper = TwitterScraper(cfg, client)
    item = _build_item_with_conversation_id("conv-1")
    out = asyncio.run(scraper.fetch_replies_for_item(item))
    assert out == []


def test_fetch_replies_happy_path_scores_and_truncates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APIFY_TOKEN", "tok")
    rows = [
        {"user": {"handle": "alice"}, "favorite_count": 100, "reply_count": 5, "text": "top"},
        {"user": {"handle": "bob"}, "favorite_count": 50, "reply_count": 2, "text": "second"},
        {"user": {"handle": "carol"}, "favorite_count": 10, "reply_count": 1, "text": "third"},
        {"user": {}, "favorite_count": 1, "reply_count": 0, "text": "anon"},
    ]
    transport = _make_apify_mock_transport(rows=rows)
    client = httpx.AsyncClient(transport=transport)
    cfg = TwitterConfig(
        fetch_reply_text=True,
        max_replies_per_tweet=2,
        reply_min_likes=5,
    )
    scraper = TwitterScraper(cfg, client)
    item = _build_item_with_conversation_id("conv-1")

    out = asyncio.run(scraper.fetch_replies_for_item(item))
    assert len(out) == 2
    assert "alice" in out[0]
    assert "100" in out[0]
    assert "bob" in out[1]
    assert "carol" not in " ".join(out)
    assert "anon" not in " ".join(out)


def test_fetch_replies_handles_no_handle_falls_back_to_anon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row without a user.handle should be credited to 'anon'."""

    monkeypatch.setenv("APIFY_TOKEN", "tok")
    rows = [
        {"user": {}, "favorite_count": 50, "reply_count": 0, "text": "no handle"},
    ]
    transport = _make_apify_mock_transport(rows=rows)
    client = httpx.AsyncClient(transport=transport)
    cfg = TwitterConfig(
        fetch_reply_text=True,
        max_replies_per_tweet=5,
        reply_min_likes=0,
    )
    scraper = TwitterScraper(cfg, client)
    item = _build_item_with_conversation_id("conv-1")
    out = asyncio.run(scraper.fetch_replies_for_item(item))
    # Emoji-codepoint comparison is platform-dependent (VS-16 variance); check semantically.
    assert len(out) == 1
    assert "@anon" in out[0]
    assert "50" in out[0]
    assert "no handle" in out[0]


# ---------------------------------------------------------------------------
# ``_scrape_user`` (PR >=5 twitter-001)
#
# Layer 1 mocked-GraphQL tests for ``TwitterScraper._scrape_user`` that drive
# the closure-captured ``graphql_tweets`` list with a synthetic
# ``_FakePage`` / ``_FakeContext`` so no Playwright, no real network, and
# the 60-second polling loop is short-circuited via patched clocks.
# ---------------------------------------------------------------------------


class _FakeResponse:
    """``page.on('response', handler)`` dispatch target exposing ``url`` + JSON body."""

    def __init__(self, url: str, body: dict[str, Any]) -> None:
        self.url = url
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


class _FakeMouse:
    """Playwright ``page.mouse`` stand-in: ``move(x, y)`` is a no-op."""

    async def move(self, x: int, y: int) -> None:
        pass


class _FakePage:
    """Minimal Playwright Page stand-in: capture ``page.on(...)`` handlers
    + dispatch canned GraphQL responses via :meth:`dispatch`.
    """

    def __init__(self) -> None:
        self._handlers: list[tuple[str, Any]] = []
        self.goto_calls: list[str] = []
        self.evaluate_calls: list[str] = []
        self.evaluate_return: Any = ""
        self.mouse = _FakeMouse()

    def on(self, event: str, handler: Any) -> None:
        self._handlers.append((event, handler))

    async def goto(self, url: str, **kw: Any) -> None:
        self.goto_calls.append(url)

    async def evaluate(self, js: str) -> Any:
        self.evaluate_calls.append(js)
        # Production calls ``page.evaluate`` with three different JS strings in
        # ``_scrape_user``'s body. Branch on the JS content rather than
        # returning ``evaluate_return`` for every call, because a single
        # unconditional return for the at_bottom check (``.
        # scrollHeight ``) would set ``at_bottom`` to a truthy non-empty string
        # and let the ``if at_bottom and time > 20: break`` branch depending on
        # the patched clock's ``time()`` return value. Returning False keeps
        # that branch inert for the dispatch tests (where the joystick-shaped
        # ``_FakeLoop`` keeps ``time()`` pinned at ``0.0``), and lets the
        # login-gate test drive the loop-exit via its own per-test
        # ``_LoopLoginGate`` clock.
        if "scrollHeight" in js:
            return False
        if "document.body" in js:
            # Used for the login-gate detection (BEFORE the while loop) AND
            # for the error-page reload detection (inside iter body). The
            # production keyword check is ``any(k in body_text.lower() ...)``
            # so any non-empty ``evaluate_return`` test override works.
            return self.evaluate_return
        # window.scrollBy result is unused by production.
        return ""

    async def route(self, pattern: str, handler: Any) -> None:
        pass

    async def reload(self, **kw: Any) -> None:
        pass

    async def close(self) -> None:
        pass

    async def dispatch(self, url: str, body: dict[str, Any]) -> None:
        """Trigger every ``response`` handler with the canned GraphQL JSON.

        Production's ``_scrape_user`` filters on ``"UserTweets" in url`` or
        ``"UserByScreenName" in url``; we use a URL that contains both.
        """
        response = _FakeResponse(url, body)
        for event_name, handler in list(self._handlers):
            if event_name == "response":
                await handler(response)


class _FakeContext:
    """Playwright Browser context stand-in: returns our single ``_FakePage``."""

    def __init__(self, page: _FakePage) -> None:
        self.page = page

    async def new_page(self) -> _FakePage:
        return self.page


def _wrap_tweet_envelope(tweet: dict[str, Any]) -> dict[str, Any]:
    """Wrap a single tweet payload in a ``UserTweets`` GraphQL envelope."""
    return {
        "data": {
            "user": {
                "result": {
                    "timeline_v2": {
                        "timeline": {
                            "instructions": [
                                {
                                    "entries": [
                                        {
                                            "content": {
                                                "itemContent": {
                                                    "tweet_results": {"result": tweet},
                                                },
                                            },
                                        },
                                    ],
                                },
                            ],
                        },
                    },
                },
            },
        },
    }


def _make_tweet_payload(
    tweet_id: str,
    text: str,
    created_at: str,
    *,
    extended_media: list[str] | None = None,
    fallback_media: list[str] | None = None,
    retweet_via: str | None = None,
) -> dict[str, Any]:
    """Build a tweet dict matching ``_scrape_user``'s ``extract_tweets`` walker."""
    legacy: dict[str, Any] = {
        "full_text": text,
        "created_at": created_at,
        "entities": {},
        "extended_entities": {},
    }
    if extended_media is not None:
        legacy["extended_entities"]["media"] = [{"type": "photo", "media_url_https": url} for url in extended_media]
    if fallback_media is not None:
        legacy["entities"]["media"] = [{"type": "photo", "media_url_https": url} for url in fallback_media]
    if retweet_via == "core":
        return {
            "rest_id": tweet_id,
            "legacy": legacy,
            "core": {"retweeted_status_result": {}},
        }
    if retweet_via == "legacy":
        legacy["retweeted_status_id_str"] = "9999"
        return {"rest_id": tweet_id, "legacy": legacy}
    return {"rest_id": tweet_id, "legacy": legacy}


@pytest.fixture
def _patch_scrape_clocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``_scrape_user`` finish in milliseconds instead of 60 seconds.

    Two patches in ``src.scrapers.twitter``:

    - ``asyncio.sleep`` becomes :func:`_no_sleep_yield` — a no-op duration that
      still yields once to the running event loop (via ``loop.call_soon``).
      This is the critical fix: a plain ``return None`` would collapse the
      test runner's ``for _ in range(N): await asyncio.sleep(0)`` yield loop
      into a no-op, preventing the scheduled ``_scrape_user`` task from ever
      running before the dispatch fires.
    - ``asyncio.get_event_loop`` returns a :class:`_FakeLoop` whose ``.time()``
      is pinned at ``0.0`` so the ``while (…) < 60`` polling bound never
      trips. The while loop terminates via the production ``return result``
      path once a dispatched response populates the captured
      ``graphql_tweets`` list. (For the no-dispatch login-gate test a
      ``at_bottom + > 20 s`` break also exists in production, but it relies
      on ``at_bottom`` which the fake cannot synthesize — the cap below
      handles that test path.)
    """

    async def _no_sleep_yield(*_a: Any, **_kw: Any) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return
        fut = running.create_future()
        running.call_soon(fut.set_result, None)
        await fut

    monkeypatch.setattr("src.scrapers.twitter.asyncio.sleep", _no_sleep_yield)

    _n = [0]

    class _FakeLoop:
        def time(self) -> float:
            _n[0] += 1
            # Pin start_time at 0 and keep every subsequent check at 0 so the
            # ``while (…) - start_time < 60`` bound is never tripped. The loop
            # exits naturally via the ``return result`` branch once a
            # dispatched GraphQL response populates ``graphql_tweets``.
            return 0.0

    monkeypatch.setattr(
        "src.scrapers.twitter.asyncio.get_event_loop",
        lambda: _FakeLoop(),
    )


async def _yield_n(times: int) -> None:
    """Yield to the running event loop ``times`` via ``loop.call_soon`` so the
    scheduled ``_scrape_user`` task actually advances between iterations. Plain
    ``await asyncio.sleep(0)`` would be short-circuited by our global
    ``asyncio.sleep`` monkey-patch.
    """
    loop = asyncio.get_running_loop()
    for _ in range(times):
        fut = loop.create_future()
        loop.call_soon(fut.set_result, None)
        await fut


def _drive_scrape_user(
    tweets: list[dict[str, Any]],
    since: datetime,
    *,
    fetch_limit: int = 10,
) -> list[dict[str, Any]] | None:
    """Synchronously drive ``_scrape_user`` + dispatch canned GraphQL mid-flight.

    Returns the list of parsed tweet dicts after time-window filter + dedup +
    ``fetch_limit`` truncation, or ``None`` if GraphQL never returned.
    """
    page = _FakePage()
    ctx = _FakeContext(page)
    cfg = TwitterConfig(fetch_limit=fetch_limit)
    scraper = TwitterScraper(cfg)
    envelopes = [_wrap_tweet_envelope(t) for t in tweets]

    async def _runner() -> list[dict[str, Any]] | None:
        task = asyncio.create_task(scraper._scrape_user(ctx, "alice", since))
        # 50 yields is overkill but cheap: production's pre-loop setup has
        # ~6 awaits (new_page, route, sleep, goto, sleep, evaluate) and the
        # first while iteration has 5 more (evaluate x2, mouse.move,
        # evaluate, sleep, evaluate). 50 tick-yields guarantees we are inside
        # the while-body when dispatch fires.
        await _yield_n(50)
        for env in envelopes:
            await page.dispatch(
                "https://x.com/i/api/graphql/x/UserTweets?variables=foo",
                env,
            )
        return await task

    return asyncio.run(_runner())


# ---------------------------------------------------------------------------
# 9 Layer 1 mocked-GraphQL tests for ``_scrape_user``
# ---------------------------------------------------------------------------


def test_scrape_user_returns_tweets_when_graphql_match(
    _patch_scrape_clocks: None,
) -> None:
    """A 1-tweet GraphQL response with a UserTweets URL → 1 parsed dict."""
    since = datetime(2026, 1, 1, tzinfo=UTC)
    out = _drive_scrape_user(
        [
            _make_tweet_payload(
                tweet_id="1001",
                text="hello world",
                created_at="Fri Jan 02 12:00:00 +0000 2026",
            ),
        ],
        since,
    )
    assert out is not None
    assert len(out) == 1
    assert out[0]["tweet_id"] == "1001"
    assert out[0]["text"] == "hello world"


def test_scrape_user_skips_tweets_outside_time_window(
    _patch_scrape_clocks: None,
) -> None:
    """A tweet older than ``since`` is dropped; one newer is kept."""
    since = datetime(2026, 1, 5, tzinfo=UTC)
    out = _drive_scrape_user(
        [
            _make_tweet_payload(
                tweet_id="OLD",
                text="old tweet",
                created_at="Thu Jan 01 12:00:00 +0000 2026",  # BEFORE since (true weekday)
            ),
            _make_tweet_payload(
                tweet_id="NEW",
                text="new tweet",
                created_at="Sat Jan 10 12:00:00 +0000 2026",  # AFTER since (true weekday)
            ),
        ],
        since,
    )
    assert out is not None
    assert len(out) == 1
    assert out[0]["tweet_id"] == "NEW"


def test_scrape_user_extracts_images_from_extended_entities(
    _patch_scrape_clocks: None,
) -> None:
    """``legacy.extended_entities.media[*].media_url_https`` populates ``images``."""
    since = datetime(2026, 1, 1, tzinfo=UTC)
    out = _drive_scrape_user(
        [
            _make_tweet_payload(
                tweet_id="1001",
                text="photo tweet",
                created_at="Fri Jan 02 12:00:00 +0000 2026",
                extended_media=[
                    "https://pbs.twimg.com/media/aaa.jpg",
                    "https://pbs.twimg.com/media/bbb.jpg",
                ],
            ),
        ],
        since,
    )
    assert out is not None
    assert out[0]["images"] == [
        "https://pbs.twimg.com/media/aaa.jpg",
        "https://pbs.twimg.com/media/bbb.jpg",
    ]


def test_scrape_user_extracts_images_from_entities_fallback(
    _patch_scrape_clocks: None,
) -> None:
    """Without ``extended_entities``, fall through to ``legacy.entities.media``."""
    since = datetime(2026, 1, 1, tzinfo=UTC)
    out = _drive_scrape_user(
        [
            _make_tweet_payload(
                tweet_id="1001",
                text="fallback photo",
                created_at="Fri Jan 02 12:00:00 +0000 2026",
                fallback_media=["https://pbs.twimg.com/media/ccc.jpg"],
            ),
        ],
        since,
    )
    assert out is not None
    assert out[0]["images"] == ["https://pbs.twimg.com/media/ccc.jpg"]


def test_scrape_user_detects_retweet_via_retweeted_status_result(
    _patch_scrape_clocks: None,
) -> None:
    """``core.retweeted_status_result`` → ``is_retweet=True``."""
    since = datetime(2026, 1, 1, tzinfo=UTC)
    out = _drive_scrape_user(
        [
            _make_tweet_payload(
                tweet_id="1001",
                text="RT something",
                created_at="Fri Jan 02 12:00:00 +0000 2026",
                retweet_via="core",
            ),
        ],
        since,
    )
    assert out is not None
    assert out[0]["is_retweet"] is True


def test_scrape_user_detects_retweet_via_legacy_marker(
    _patch_scrape_clocks: None,
) -> None:
    """``legacy.retweeted_status_id_str`` → ``is_retweet=True`` (no core tag)."""
    since = datetime(2026, 1, 1, tzinfo=UTC)
    out = _drive_scrape_user(
        [
            _make_tweet_payload(
                tweet_id="1001",
                text="RT legacy",
                created_at="Fri Jan 02 12:00:00 +0000 2026",
                retweet_via="legacy",
            ),
        ],
        since,
    )
    assert out is not None
    assert out[0]["is_retweet"] is True


def test_scrape_user_dedupes_by_tweet_id(_patch_scrape_clocks: None) -> None:
    """Two tweets with the same ``rest_id`` → only one entry survives."""
    since = datetime(2026, 1, 1, tzinfo=UTC)
    payload = _make_tweet_payload(
        tweet_id="DUPE",
        text="dup",
        created_at="Fri Jan 02 12:00:00 +0000 2026",  # 2026-01-02 is a Friday
    )
    out = _drive_scrape_user([payload, payload], since)
    assert out is not None
    assert len(out) == 1
    assert out[0]["tweet_id"] == "DUPE"


def test_scrape_user_truncates_to_fetch_limit(_patch_scrape_clocks: None) -> None:
    """5 valid tweets with ``fetch_limit=3`` → only 3 returned."""
    since = datetime(2026, 1, 1, tzinfo=UTC)
    out = _drive_scrape_user(
        [
            _make_tweet_payload(
                tweet_id=f"1{i}",
                text=f"tweet {i}",
                created_at="Fri Jan 02 12:00:00 +0000 2026",
            )
            for i in range(5)
        ],
        since,
        fetch_limit=3,
    )
    assert out is not None
    assert len(out) == 3


def test_scrape_user_logs_login_gate(
    _patch_scrape_clocks: None,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Body text containing login-gate keyword → warning logged + result is ``None``.

    No GraphQL response is dispatched, so the loop must exit via the
    ``while (...) < 60`` time bound (NOT via the ``if graphql_tweets``
    payload path) so production's post-loop ``if not graphql_tweets:
    return None`` branch fires. The fixture's ``_FakeLoop`` pins
    ``time()`` at ``0.0`` for the dispatch tests, so we override the
    ``asyncio.get_event_loop`` attribute per-test to advance time, taking
    the loop out in 2 iterations.
    """
    page = _FakePage()
    page.evaluate_return = "Please log in to continue"
    ctx = _FakeContext(page)
    cfg = TwitterConfig()
    scraper = TwitterScraper(cfg)
    since = datetime(2026, 1, 1, tzinfo=UTC)

    _n_lg = [0]

    class _LoopLoginGate:
        def time(self) -> float:
            _n_lg[0] += 1
            # 1st call: start_time = 0
            # 2nd call: iter 1 while-check = 30 (30 - 0 < 60 → enter)
            # 3rd call: would be at_bottom check (skipped because
            # ``_FakePage.evaluate`` returns False for ``scrollHeight`` JS)
            # 4th call: iter 2 while-check = 90 (90 - 0 NOT < 60 → EXIT)
            # 5th call: never reached.
            return (_n_lg[0] - 1) * 30.0

    monkeypatch.setattr(
        "src.scrapers.twitter.asyncio.get_event_loop",
        lambda: _LoopLoginGate(),
    )

    async def _runner() -> list[dict[str, Any]] | None:
        return await scraper._scrape_user(ctx, "alice", since)

    with caplog.at_level("WARNING", logger="src.scrapers.twitter"):
        out = asyncio.run(_runner())

    assert out is None
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "login gate" in messages.lower()
