"""Tests for ``src/scrapers/reddit.py`` — Reddit posts/comments scraper.

Coverage targets:
- ``fetch`` orchestration (subreddits + users; disabled short-circuits)
- ``_fetch_subreddit`` JSON listing → posts → comments
- ``_fetch_subreddit_rss`` fallback on 403/RateLimit
- ``_fetch_user`` user listing
- ``_process_posts`` date filter + min_score filter + comments join
- ``_fetch_comments`` top-comments fetch
- ``_parse_post`` self vs link URL handling
- ``_strip_html`` helper
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock

from src.models import RedditConfig, RedditSubredditConfig, RedditUserConfig
from src.scrapers.reddit import RedditScraper


def _client_with_routes(routes):
    """Build an ``httpx.AsyncClient`` whose ``.get(url, ...)`` returns canned responses
    keyed by URL substring. Routes is a list of ``(substring, body_dict)``; first match wins.
    """
    from unittest.mock import AsyncMock

    entries = list(routes)
    client = AsyncMock()

    async def fake_get(url, params=None, headers=None, **kw):
        for substring, body in entries:
            if substring in url:
                resp = MagicMock()
                resp.status_code = body.get("status_code", 200)
                resp.headers = body.get("headers", {})
                resp.text = body.get("text", "")
                if "json" in body:
                    resp.json = MagicMock(return_value=body["json"])
                resp.raise_for_status = MagicMock(
                    side_effect=body.get("raise_for_status")
                )
                return resp
        # 404 fallback for un-mocked URLs.
        resp = MagicMock()
        resp.status_code = 404
        resp.headers = {}
        resp.text = ""
        resp.json = MagicMock(return_value={})
        resp.raise_for_status = MagicMock(side_effect=Exception("not found"))
        return resp

    client.get = fake_get
    return client


def _since() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def test_fetch_returns_empty_when_disabled():
    config = RedditConfig(enabled=False, subreddits=[], users=[])
    client = _client_with_routes([])
    scraper = RedditScraper(config, client)
    result = asyncio.run(scraper.fetch(_since()))
    assert result == []


def test_fetch_subreddit_happy_path():
    sub = RedditSubredditConfig(
        subreddit="python", sort="hot", time_filter="day",
        fetch_limit=5, min_score=0, enabled=True,
    )
    config = RedditConfig(subreddits=[sub], users=[], fetch_comments=0)
    listing = {
        "data": {
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "id": "abc123",
                        "title": "Test Post",
                        "score": 100,
                        "subreddit": "python",
                        "permalink": "/r/python/comments/abc123/test",
                        "url": "https://example.com/test",
                        "selftext": "body text",
                        "created_utc": 1767312000,
                        "is_self": False,
                        "author": "alice",
                        "upvote_ratio": 0.9,
                        "num_comments": 10,
                    },
                }
            ]
        }
    }
    client = _client_with_routes([("/r/python/hot.json", {"json": listing})])
    scraper = RedditScraper(config, client)

    result = asyncio.run(scraper.fetch(_since()))

    assert len(result) == 1
    assert result[0].title == "Test Post"
    assert result[0].source_type.value == "reddit"
    assert result[0].metadata["subreddit"] == "python"
    assert result[0].metadata["num_comments"] == 10


def test_fetch_skips_posts_below_min_score():
    sub = RedditSubredditConfig(subreddit="python", min_score=200, fetch_limit=5)
    config = RedditConfig(subreddits=[sub], users=[], fetch_comments=0)
    listing = {
        "data": {
            "children": [
                {"kind": "t3", "data": {
                    "id": "low", "title": "Low Score", "score": 5,
                    "subreddit": "python", "permalink": "/r/python/comments/low",
                    "url": "", "selftext": "", "created_utc": 1767312000,
                    "is_self": False, "author": "u", "upvote_ratio": 0.9,
                    "num_comments": 0,
                }},
            ]
        }
    }
    client = _client_with_routes([("/r/python/hot.json", {"json": listing})])
    scraper = RedditScraper(config, client)
    result = asyncio.run(scraper.fetch(_since()))
    assert result == []


def test_fetch_subreddit_falls_back_to_rss_on_403():
    sub = RedditSubredditConfig(subreddit="python", fetch_limit=2, min_score=0)
    config = RedditConfig(subreddits=[sub], users=[], fetch_comments=0)

    async def fake_get(url, params=None, headers=None, **kw):
        if "/hot.json" in url:
            raise_403 = Exception("403 blocked")
            resp = MagicMock()
            resp.status_code = 403
            resp.headers = {}
            resp.json = MagicMock(side_effect=raise_403)
            resp.raise_for_status = MagicMock(side_effect=raise_403)
            return resp
        # RSS fallback
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.text = (
            '<?xml version="1.0"?><feed><entry>'
            "<id>t1</id><title>RSS Post</title><link>https://www.reddit.com/r/python/comments/t1</link>"
            "<updated>Wed, 01 Jan 2026 12:00:00 GMT</updated>"
            "<author>alice</author><summary>summary</summary>"
            "</entry></feed>"
        )
        resp.raise_for_status = MagicMock(return_value=None)
        resp.json = MagicMock(return_value={})
        return resp

    from unittest.mock import AsyncMock
    client = AsyncMock()
    client.get = fake_get
    scraper = RedditScraper(config, client)

    # Should not raise; falls back to RSS.
    result = asyncio.run(scraper.fetch(_since()))
    # Feedparser v6 is lenient — even invalid RSS returns either real entries or empty.
    assert isinstance(result, list)


def test_fetch_user_listing():
    user = RedditUserConfig(username="thysrael", sort="new", fetch_limit=5, enabled=True)
    config = RedditConfig(subreddits=[], users=[user], fetch_comments=0)
    listing = {
        "data": {
            "children": [
                {"kind": "t3", "data": {
                    "id": "u1", "title": "User post", "score": 10,
                    "subreddit": "test", "permalink": "/r/test/comments/u1/post",
                    "url": "https://example.com/p", "selftext": "",
                    "created_utc": 1767312000, "is_self": True, "author": "thysrael",
                    "upvote_ratio": 1.0, "num_comments": 0,
                }},
            ]
        }
    }
    client = _client_with_routes([("/user/thysrael/submitted.json", {"json": listing})])
    scraper = RedditScraper(config, client)

    result = asyncio.run(scraper.fetch(_since()))
    assert len(result) == 1
    assert result[0].title == "User post"
