"""Tests for TwitterScraper.

Test layer split
================
- Layer 1 (no Playwright required, runs in default CI):
    Pure unit tests over ``_parse_tweet`` and ``fetch()`` early-return paths.
    No httpx mock, no chromium, no async network. Exercises the contract
    that the production Playwright + GraphQL interception implementation
    exposes without spinning up a browser.
- Layer 2 (``@pytest.mark.requires_playwright``, PR 2):
    Real headless chromium with routed GraphQL responses to bypass x.com.
    Will be gated by a separate nightly CI job.

Reply/discussion/negative-branch tests pre-date this split. They exercise the
still-Apify-based ``fetch_replies_for_item`` path and remain unchanged.
"""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

try:
    from playwright.async_api import async_playwright as _async_playwright
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _async_playwright = None
    _PLAYWRIGHT_AVAILABLE = False

from src.models import TwitterConfig
from src.scrapers.twitter import TwitterScraper


def _make_config(**kwargs) -> TwitterConfig:
    defaults = {
        "enabled": True,
        "users": ["karpathy"],
        "fetch_limit": 3,
        "actor_id": "altimis~scweet",
        "apify_token_env": "APIFY_TOKEN",
    }
    defaults.update(kwargs)
    return TwitterConfig(**defaults)


def _reply_row(
    tweet_id: str = "999",
    handle: str = "someone",
    text: str = "Great point!",
    likes: int = 5,
) -> dict:
    now = datetime.now(UTC)
    return {
        "id": f"tweet-{tweet_id}",
        "handle": handle,
        "text": text,
        "favorite_count": likes,
        "reply_count": 0,
        "created_at": now.strftime("%a %b %d %H:%M:%S +0000 %Y"),
        "user": {"handle": handle, "name": handle},
    }


def _run_resp(run_id="run1", dataset_id="ds1"):
    return {"data": {"id": run_id, "defaultDatasetId": dataset_id}}


def _status_resp(status="SUCCEEDED"):
    return {"data": {"status": status}}


def _make_graphql_payload(
    tweet_id: str,
    text: str,
    created_at_raw: str,
    *,
    is_retweet: bool = False,
) -> dict:
    """Build a single tweet at the shape ``extract_tweets`` looks for in ``_scrape_user``.

    ``extract_tweets`` in ``src/scrapers/twitter.py`` recursively walks the JSON
    and matches any object containing both ``rest_id`` and ``legacy`` keys.
    Plus it inspects ``core.retweeted_status_result`` for the ``is_retweet`` flag.
    """
    payload: dict = {
        "rest_id": str(tweet_id),
        "legacy": {
            "full_text": text,
            "created_at": created_at_raw,
            "entities": {"media": []},
            "extended_entities": {"media": []},
        },
    }
    if is_retweet:
        payload["core"] = {"retweeted_status_result": {}}
    return payload


def _wrap_graphql_response(*tweets: dict) -> dict:
    """Wrap one or more tweet payloads in a minimal X.com timeline envelope.

    The recursive walker accepts any nesting, so this wrapper only needs to
    deliver the tweets somewhere reachable. Matches the shape typical replies
    from ``UserTweets`` GraphQL operations.
    """
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
                                                    "tweet_results": {"result": t},
                                                },
                                            },
                                        }
                                        for t in tweets
                                    ],
                                },
                            ],
                        }
                    }
                }
            }
        }
    }


def _write_fake_cookie_file(tmp_path) -> None:
    """Write one fake JSON cookie so ``_load_browser_cookies`` returns >=1 row."""
    cookie_file = tmp_path / "x_cookies_1.json"
    cookie_file.write_text(
        json.dumps(
            [
                {
                    "name": "auth_token",
                    "value": "stub",
                    "domain": ".x.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                },
            ]
        )
    )


# ---------------------------------------------------------------------------
# Layer 1 — fetch() early-return paths (no Playwright, no httpx mock)
# ---------------------------------------------------------------------------


def test_fetch_disabled_returns_empty():
    """``config.enabled=False`` short-circuits before any network call."""
    scraper = TwitterScraper(_make_config(enabled=False))
    result = asyncio.run(scraper.fetch(datetime.now(UTC)))
    assert result == []


def test_fetch_no_users_returns_empty():
    """Empty / whitespace / @-only users lists are filtered to "" and short-circuit."""
    for users in ([], ["", "  ", "@"], ["   "]):
        scraper = TwitterScraper(_make_config(users=list(users)))
        result = asyncio.run(scraper.fetch(datetime.now(UTC)))
        assert result == [], f"users={users!r} should yield []"


def test_fetch_no_cookie_files_returns_empty(tmp_path, monkeypatch):
    """With Playwright installed but no cookie files in ``cookie_dir``, fetch() returns []."""
    monkeypatch.setattr("src.scrapers.twitter.PLAYWRIGHT_AVAILABLE", True)
    cfg = _make_config(cookie_dir=str(tmp_path), cookie_file_pattern="*.json")
    scraper = TwitterScraper(cfg)
    result = asyncio.run(scraper.fetch(datetime.now(UTC)))
    assert result == []


def test_fetch_no_playwright_returns_empty(monkeypatch):
    """When ``PLAYWRIGHT_AVAILABLE`` is False, fetch() returns [] and skips the entire lifecycle."""
    monkeypatch.setattr("src.scrapers.twitter.PLAYWRIGHT_AVAILABLE", False)
    scraper = TwitterScraper(_make_config())
    result = asyncio.run(scraper.fetch(datetime.now(UTC)))
    assert result == []


# ---------------------------------------------------------------------------
# Layer 1 — _parse_tweet contract (GraphQL-shape input)
# ---------------------------------------------------------------------------


def test_parse_tweet_metadata_keys_aligned_for_analyzer():
    """``_parse_tweet`` writes the keys the downstream analyzer reads.

    Note: the ``datetime`` field is the **post-`_scrape_user`-strptime** ISO form,
    which is the shape ``_scrape_user`` writes into the dict (see
    ``extract_tweets`` in twitter.py) before ``_parse_tweet`` consumes it.
    """
    scraper = TwitterScraper(_make_config())
    raw = {
        "tweet_id": "42",
        "text": "Hello world",
        "datetime": "2024-01-01T12:00:00+00:00",
        "datetime_raw": "Mon Jan 01 12:00:00 +0000 2024",
        "is_retweet": False,
        "images": [],
    }
    item = scraper._parse_tweet(raw, "karpathy")
    assert item is not None
    assert item.metadata["tweet_id"] == "42"
    assert item.metadata["is_retweet"] is False
    assert item.metadata["images"] == []
    assert item.author == "karpathy"
    assert item.source_type.value == "twitter"
    assert item.id == "twitter:tweet:42"


def test_parse_tweet_invalid_dict_returns_none():
    """``_parse_tweet`` returns ``None`` for any malformed input — no raise, no crash."""
    scraper = TwitterScraper(_make_config())
    cases = [
        ({}, "empty dict"),
        ({"text": "missing id"}, "missing tweet_id"),
        ({"tweet_id": "1"}, "missing text"),
        ({"tweet_id": "1", "text": "ok", "datetime": "not-a-date"}, "invalid datetime"),
    ]
    for raw, scenario in cases:
        assert scraper._parse_tweet(raw, "x") is None, scenario


def test_parse_tweet_accepts_iso_datetime():
    """``_parse_tweet`` accepts ISO 8601 datetime strings — Layer 1 half of the
    filters_old_tweets contract (time parsing must succeed before
    ``_scrape_user`` can compare ``published_at < since``).
    """
    scraper = TwitterScraper(_make_config())
    raw = {
        "tweet_id": "77",
        "text": "iso tweet",
        "datetime": "2024-01-01T12:00:00+00:00",
        "datetime_raw": "Mon Jan 01 12:00:00 +0000 2024",
        "is_retweet": False,
        "images": [],
    }
    item = scraper._parse_tweet(raw, "x")
    assert item is not None
    assert item.published_at.year == 2024
    assert item.published_at.tzinfo is not None


def test_parse_tweet_url_constructed_when_missing():
    """``_parse_tweet`` synthesises the x.com status URL when none is provided."""
    scraper = TwitterScraper(_make_config())
    raw = {
        "tweet_id": "55",
        "text": "url test",
        "datetime": "2024-01-01T12:00:00+00:00",
        "datetime_raw": "Mon Jan 01 12:00:00 +0000 2024",
        "is_retweet": False,
        "images": [],
    }
    item = scraper._parse_tweet(raw, "testuser")
    assert item is not None
    assert "testuser" in str(item.url)
    assert "55" in str(item.url)


# ---------------------------------------------------------------------------
# Reply fetch tests (Apify REST contract — unchanged)
# ---------------------------------------------------------------------------


def test_fetch_replies_disabled_by_default():
    """fetch_reply_text defaults to False — verify config default."""
    cfg = TwitterConfig()
    assert cfg.fetch_reply_text is False


def test_fetch_replies_appends_top_comments(monkeypatch):
    """When fetch_reply_text=True, reply lines are appended under Top Comments."""
    monkeypatch.setenv("APIFY_TOKEN", "test_token")

    replies = [
        _reply_row("r1", "alice", "Interesting take!", likes=20),
        _reply_row("r2", "bob", "I disagree because...", likes=5),
        _reply_row("r3", "carol", "Great point!", likes=15),
        _reply_row("r4", "dave", "Low quality reply", likes=0),
    ]

    run_counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/runs" in request.url.path and request.method == "POST":
            run_counter["n"] += 1
            ds = f"ds{run_counter['n']}"
            return httpx.Response(200, json=_run_resp(f"run{run_counter['n']}", ds))
        if "/actor-runs/" in request.url.path:
            return httpx.Response(200, json=_status_resp())
        if "/datasets/" in request.url.path:
            return httpx.Response(200, json=replies)
        raise AssertionError(f"Unexpected: {request.url}")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    cfg = _make_config(
        fetch_reply_text=True,
        max_replies_per_tweet=3,
        reply_min_likes=1,
    )
    scraper = TwitterScraper(cfg, client)

    from datetime import datetime

    from src.models import ContentItem, SourceType

    item = ContentItem(
        id="twitter:tweet:42",
        source_type=SourceType.TWITTER,
        title="@karpathy: test tweet",
        url="https://twitter.com/karpathy/status/42",
        content="test tweet body",
        author="Andrej Karpathy",
        published_at=datetime.now(UTC),
        metadata={"tweet_id": "42", "conversation_id": "42"},
    )

    reply_lines = asyncio.run(scraper.fetch_replies_for_item(item))
    asyncio.run(client.aclose())

    # min_likes=1 filters out dave (0 likes); max 3 returned sorted by score
    assert len(reply_lines) == 3
    # alice (20 likes) should be first
    assert "alice" in reply_lines[0]
    assert "Interesting take!" in reply_lines[0]
    # dave (0 likes) filtered out
    assert not any("dave" in line for line in reply_lines)


def test_append_discussion_content_adds_marker():
    from src.models import ContentItem, SourceType

    item = ContentItem(
        id="twitter:tweet:1",
        source_type=SourceType.TWITTER,
        title="test",
        url="https://twitter.com/x/status/1",
        content="original text",
        author="x",
        published_at=datetime.now(UTC),
        metadata={},
    )
    changed = TwitterScraper.append_discussion_content(item, ["[@alice | ❤️ 5 | 💬 1] reply text"])
    assert changed is True
    assert "--- Top Comments ---" in item.content
    assert "alice" in item.content


def test_append_discussion_content_empty_lines_no_change():
    from src.models import ContentItem, SourceType

    item = ContentItem(
        id="twitter:tweet:2",
        source_type=SourceType.TWITTER,
        title="test",
        url="https://twitter.com/x/status/2",
        content="original",
        author="x",
        published_at=datetime.now(UTC),
        metadata={},
    )
    changed = TwitterScraper.append_discussion_content(item, [])
    assert changed is False
    assert item.content == "original"


def test_fetch_replies_no_conversation_id_returns_empty(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "test_token")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    client = httpx.AsyncClient(transport=transport)
    cfg = _make_config(fetch_reply_text=True)
    scraper = TwitterScraper(cfg, client)

    from src.models import ContentItem, SourceType

    item = ContentItem(
        id="twitter:tweet:x",
        source_type=SourceType.TWITTER,
        title="test",
        url="https://twitter.com/x/status/x",
        content="body",
        author="x",
        published_at=datetime.now(UTC),
        metadata={},  # no conversation_id
    )
    result = asyncio.run(scraper.fetch_replies_for_item(item))
    asyncio.run(client.aclose())
    assert result == []


# ---------------------------------------------------------------------------
# Negative-branch coverage for fetch_replies_for_item (unchanged)
# ---------------------------------------------------------------------------


def test_fetch_replies_run_start_5xx_returns_empty(monkeypatch):
    """HTTP 500 on POST /runs short-circuits and never makes further calls."""
    monkeypatch.setenv("APIFY_TOKEN", "test_token")
    actor_id = _make_config().actor_id
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.path)
        if "/runs" in request.url.path and request.method == "POST":
            return httpx.Response(500, json={"error": "upstream"})
        raise AssertionError(f"Unexpected call after 5xx: {request.url}")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    cfg = _make_config(fetch_reply_text=True)
    scraper = TwitterScraper(cfg, client)

    from src.models import ContentItem, SourceType  # noqa: PLC0415

    item = ContentItem(
        id="twitter:tweet:42",
        source_type=SourceType.TWITTER,
        title="t",
        url="https://twitter.com/x/status/42",
        content="body",
        author="x",
        published_at=datetime.now(UTC),
        metadata={"tweet_id": "42", "conversation_id": "42"},
    )
    result = asyncio.run(scraper.fetch_replies_for_item(item))
    asyncio.run(client.aclose())

    assert result == []
    assert captured == [f"/v2/acts/{actor_id}/runs"]


def test_fetch_replies_status_not_succeeded_returns_empty(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "test_token")

    def handler(request: httpx.Request) -> httpx.Response:
        if "/runs" in request.url.path and request.method == "POST":
            return httpx.Response(200, json=_run_resp())
        if "/actor-runs/" in request.url.path:
            return httpx.Response(200, json=_status_resp("FAILED"))
        raise AssertionError(f"Unexpected: {request.url}")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    cfg = _make_config(fetch_reply_text=True)
    scraper = TwitterScraper(cfg, client)

    from src.models import ContentItem, SourceType  # noqa: PLC0415

    item = ContentItem(
        id="twitter:tweet:42",
        source_type=SourceType.TWITTER,
        title="t",
        url="https://twitter.com/x/status/42",
        content="body",
        author="x",
        published_at=datetime.now(UTC),
        metadata={"tweet_id": "42", "conversation_id": "42"},
    )
    result = asyncio.run(scraper.fetch_replies_for_item(item))
    asyncio.run(client.aclose())
    assert result == []


def test_fetch_replies_missing_run_id_in_response_returns_empty(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "test_token")

    def handler(request: httpx.Request) -> httpx.Response:
        if "/runs" in request.url.path and request.method == "POST":
            return httpx.Response(200, json={"data": {}})  # no id / defaultDatasetId
        raise AssertionError(f"Unexpected: {request.url}")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    cfg = _make_config(fetch_reply_text=True)
    scraper = TwitterScraper(cfg, client)

    from src.models import ContentItem, SourceType  # noqa: PLC0415

    item = ContentItem(
        id="twitter:tweet:42",
        source_type=SourceType.TWITTER,
        title="t",
        url="https://twitter.com/x/status/42",
        content="body",
        author="x",
        published_at=datetime.now(UTC),
        metadata={"tweet_id": "42", "conversation_id": "42"},
    )
    result = asyncio.run(scraper.fetch_replies_for_item(item))
    asyncio.run(client.aclose())
    assert result == []


def test_fetch_replies_empty_dataset_returns_empty(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "test_token")

    def handler(request: httpx.Request) -> httpx.Response:
        if "/runs" in request.url.path and request.method == "POST":
            return httpx.Response(200, json=_run_resp())
        if "/actor-runs/" in request.url.path:
            return httpx.Response(200, json=_status_resp())
        if "/datasets/" in request.url.path:
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected: {request.url}")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    cfg = _make_config(fetch_reply_text=True)
    scraper = TwitterScraper(cfg, client)

    from src.models import ContentItem, SourceType  # noqa: PLC0415

    item = ContentItem(
        id="twitter:tweet:42",
        source_type=SourceType.TWITTER,
        title="t",
        url="https://twitter.com/x/status/42",
        content="body",
        author="x",
        published_at=datetime.now(UTC),
        metadata={"tweet_id": "42", "conversation_id": "42"},
    )
    result = asyncio.run(scraper.fetch_replies_for_item(item))
    asyncio.run(client.aclose())
    assert result == []


def test_fetch_replies_all_below_min_likes_returns_empty(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "test_token")
    replies = [
        _reply_row("r1", "alice", "low likes", likes=1),
        _reply_row("r2", "bob", "less", likes=0),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if "/runs" in request.url.path and request.method == "POST":
            return httpx.Response(200, json=_run_resp())
        if "/actor-runs/" in request.url.path:
            return httpx.Response(200, json=_status_resp())
        if "/datasets/" in request.url.path:
            return httpx.Response(200, json=replies)
        raise AssertionError(f"Unexpected: {request.url}")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    cfg = _make_config(fetch_reply_text=True, reply_min_likes=10)
    scraper = TwitterScraper(cfg, client)

    from src.models import ContentItem, SourceType  # noqa: PLC0415

    item = ContentItem(
        id="twitter:tweet:42",
        source_type=SourceType.TWITTER,
        title="t",
        url="https://twitter.com/x/status/42",
        content="body",
        author="x",
        published_at=datetime.now(UTC),
        metadata={"tweet_id": "42", "conversation_id": "42"},
    )
    result = asyncio.run(scraper.fetch_replies_for_item(item))
    asyncio.run(client.aclose())
    assert result == []


# ---------------------------------------------------------------------------
# Layer 2 — fetch() end-to-end with real chromium
# ---------------------------------------------------------------------------


@pytest.mark.requires_playwright
def test_fetch_end_to_end_routes_graphql(tmp_path, fast_twitter_waits, monkeypatch):
    """Real chromium + routed GraphQL: ``fetch`` returns 1 parsed ContentItem.

    Uses ``monkeypatch.setattr(scraper, "_scrape_user", wrapper)`` to inject
    ``ctx.route()`` handlers before delegating to the real implementation —
    keeps the seam at production boundaries (no internals poking).
    """
    if not _PLAYWRIGHT_AVAILABLE:
        pytest.skip(
            "Playwright Python package not installed; run: uv sync --extra twitter"
        )

    _write_fake_cookie_file(tmp_path)
    cfg = TwitterConfig(
        enabled=True,
        users=["testuser"],
        cookie_dir=str(tmp_path),
        cookie_file_pattern="x_cookies_*.json",
        fetch_limit=3,
    )
    scraper = TwitterScraper(cfg)
    canned = _wrap_graphql_response(
        _make_graphql_payload("101", "New tweet", "Wed Jan 01 12:00:00 +0000 2025"),
    )

    orig_scrape = scraper._scrape_user

    async def _mock_scrape(ctx, username, since):
        await ctx.route(
            "**/UserTweets*",
            lambda route: route.fulfill(json=canned, status=200),
        )
        await ctx.route(
            "**/UserByScreenName*",
            lambda route: route.fulfill(
                json={"data": {"user": {"result": {"rest_id": "stub", "legacy": {}}}}},
                status=200,
            ),
        )
        # Stub the x.com HTML shell so page.goto + the polling loop's body_text
        # evaluate don't depend on real network state.
        await ctx.route(
            "https://x.com/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body="<!DOCTYPE html><html><body><div>stub</div></body></html>",
            ),
        )
        return await orig_scrape(ctx, username, since)

    monkeypatch.setattr(scraper, "_scrape_user", _mock_scrape)

    monkeypatch.setattr(scraper, "_warm_up_contexts", lambda _contexts: asyncio.sleep(0))  # PR 3 seam

    async def _run():
        return await scraper.fetch(datetime.fromisoformat("2024-01-01T00:00:00+00:00"))

    items = asyncio.run(_run())
    assert len(items) == 1
    assert items[0].metadata["tweet_id"] == "101"
    assert "New tweet" in items[0].content


@pytest.mark.requires_playwright
def test_filters_old_tweets_layer2(tmp_path, fast_twitter_waits, monkeypatch):
    """Layer 2 half of the filters_old contract: a pre-`since` tweet is filtered
    out by ``_scrape_user`` even when GraphQL returns it.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        pytest.skip(
            "Playwright Python package not installed; run: uv sync --extra twitter"
        )

    _write_fake_cookie_file(tmp_path)
    cfg = TwitterConfig(
        enabled=True,
        users=["testuser"],
        cookie_dir=str(tmp_path),
        cookie_file_pattern="x_cookies_*.json",
        fetch_limit=3,
    )
    scraper = TwitterScraper(cfg)
    canned = _wrap_graphql_response(
        _make_graphql_payload("101", "New tweet", "Wed Jan 01 12:00:00 +0000 2025"),
        _make_graphql_payload("102", "Old tweet", "Sun Dec 31 12:00:00 +0000 2023"),
    )

    orig_scrape = scraper._scrape_user

    async def _mock_scrape(ctx, username, since):
        await ctx.route(
            "**/UserTweets*",
            lambda route: route.fulfill(json=canned, status=200),
        )
        await ctx.route(
            "**/UserByScreenName*",
            lambda route: route.fulfill(
                json={"data": {"user": {"result": {"rest_id": "stub", "legacy": {}}}}},
                status=200,
            ),
        )
        await ctx.route(
            "https://x.com/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body="<!DOCTYPE html><html><body><div>stub</div></body></html>",
            ),
        )
        return await orig_scrape(ctx, username, since)

    monkeypatch.setattr(scraper, "_scrape_user", _mock_scrape)

    monkeypatch.setattr(scraper, "_warm_up_contexts", lambda _contexts: asyncio.sleep(0))  # PR 3 seam

    async def _run():
        return await scraper.fetch(datetime.fromisoformat("2024-01-01T00:00:00+00:00"))

    items = asyncio.run(_run())
    assert len(items) == 1
    assert items[0].metadata["tweet_id"] == "101"


@pytest.mark.requires_playwright
def test_scrape_page_error_returns_failed_users_layer2(tmp_path, fast_twitter_waits, monkeypatch):
    """When ``_scrape_user`` returns ``None``, ``fetch`` queues the user for
    retry; after both passes fail to collect tweets it returns ``[]``.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        pytest.skip(
            "Playwright Python package not installed; run: uv sync --extra twitter"
        )

    _write_fake_cookie_file(tmp_path)
    cfg = TwitterConfig(
        enabled=True,
        users=["testuser"],
        cookie_dir=str(tmp_path),
        cookie_file_pattern="x_cookies_*.json",
        fetch_limit=3,
    )
    scraper = TwitterScraper(cfg)

    async def _mock_scrape(*_args, **_kwargs):
        return None

    monkeypatch.setattr(scraper, "_scrape_user", _mock_scrape)

    monkeypatch.setattr(scraper, "_warm_up_contexts", lambda _contexts: asyncio.sleep(0))  # PR 3 seam

    async def _run():
        return await scraper.fetch(datetime.now(UTC))

    items = asyncio.run(_run())
    assert items == []
