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
    base["datetime"] = datetime.strptime(
        base["datetime_raw"], "%a %b %d %H:%M:%S %z %Y"
    ).isoformat()
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
    assert TwitterScraper.append_discussion_content(
        item, ["[@a | \u2764 5] great take", "[@b | \u2764 1] ok"]
    ) is True
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
    cfg = TwitterConfig(fetch_reply_text=False, conversation_id="abc")
    client = MagicMock(spec=httpx.AsyncClient)
    scraper = TwitterScraper(cfg, client)
    item = _build_item_with_conversation_id("conv-1")
    out = asyncio.run(scraper.fetch_replies_for_item(item))
    # Emoji codepoint comparison is platform-dependent (VS-16 variance).
    # Assert semantically: a single reply credited to ``anon`` with expected text + counts.
    assert len(out) == 1
    assert "@anon" in out[0]
    assert "50" in out[0]
    assert "no handle" in out[0]
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
    cfg = TwitterConfig(
        fetch_reply_text=True, max_replies_per_tweet=0, reply_min_likes=0
    )
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
        {"user": {"handle": "bob"},   "favorite_count": 50,  "reply_count": 2, "text": "second"},
        {"user": {"handle": "carol"}, "favorite_count": 10,  "reply_count": 1, "text": "third"},
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

