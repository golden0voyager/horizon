"""Tests for ``src/scrapers/hackernews.py`` — Hacker News stories + top comments.

Coverage targets:
- ``fetch`` — topstories → stories → comments fan-out
- ``_fetch_story`` — handles 404/HTTPError gracefully
- ``_fetch_comments`` — top-N kids fetch + deleted/dead filter
- ``_parse_story`` — title/author/url + comments concat

Date + score filtering. The fixture uses a fixed ``since`` so all
fixture dates can be crafted deterministically.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx

from src.models import HackerNewsConfig
from src.scrapers.hackernews import HackerNewsScraper


def _since() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def _client_with_routes(topstories_response, items_by_id, *, topstories_error=False):
    """Build an httpx client routing:
    - /topstories.json  -> topstories_response (or 500 if topstories_error=True)
    - /item/{N}.json   -> items_by_id[str(N)]
    """
    async def fake_get(url, params=None, headers=None, **kw):
        resp = MagicMock()
        resp.headers = {}
        resp.text = ""
        if topstories_error or "topstories.json" in url:
            resp.status_code = 500 if topstories_error else 200
            if topstories_error:
                resp.raise_for_status = MagicMock(side_effect=httpx.HTTPError("server error"))
            else:
                resp.raise_for_status = MagicMock(return_value=None)
            resp.json = MagicMock(return_value=topstories_response)
            return resp
        if "/item/" in url:
            try:
                story_id = int(url.rsplit("/", 1)[-1].rsplit(".", 1)[0])
            except (IndexError, ValueError):
                story_id = None
            if story_id is None or str(story_id) not in items_by_id:
                resp.status_code = 404
                resp.raise_for_status = MagicMock(side_effect=httpx.HTTPError("not found"))
                resp.json = MagicMock(return_value=None)
                return resp
            resp.status_code = 200
            resp.raise_for_status = MagicMock(return_value=None)
            resp.json = MagicMock(return_value=items_by_id[str(story_id)])
            return resp
        raise AssertionError(f"Unmocked URL: {url}")
    client = AsyncMock()
    client.get = fake_get
    return client

def test_fetch_returns_empty_when_disabled():
    config = HackerNewsConfig(enabled=False, fetch_top_stories=5, min_score=0)
    scraper = HackerNewsScraper(config, AsyncMock())
    result = asyncio.run(scraper.fetch(_since()))
    assert result == []


def test_fetch_processes_stories():
    topstories = [101, 102, 103]
    # String keys required by _client_with_routes' ``items_by_id[str(story_id)]`` lookup.
    items = {
        "101": {"id": 101, "title": "Story 1", "score": 200, "time": 1767312000,
                "by": "alice", "kids": [901, 902], "type": "story"},
        "102": {"id": 102, "title": "Story 2", "score": 50, "time": 1767312000,
                "by": "bob", "kids": [], "type": "story"},
        "901": {"id": 901, "text": "Great post!", "by": "commenter1"},
        "902": {"id": 902, "text": "<p>HTML comment</p>", "by": "commenter2"},
    }
    client = _client_with_routes(topstories, items)
    config = HackerNewsConfig(fetch_top_stories=10, min_score=100)
    scraper = HackerNewsScraper(config, client)

    result = asyncio.run(scraper.fetch(_since()))

    # Story 102 has score=50 < 100 → filtered. 103 isn't in items_by_id so it's silently dropped.
    assert len(result) == 1
    assert result[0].title == "Story 1"
    assert "--- Top Comments ---" in (result[0].content or "")
    assert "Great post!" in (result[0].content or "")
    assert "HTML comment" in (result[0].content or "")


def test_fetch_filters_stories_below_min_score():
    topstories = [201]
    items = {"201": {"id": 201, "title": "Low", "score": 5, "time": 1767312000,
                    "by": "bob", "type": "story"}}
    client = _client_with_routes(topstories, items)
    config = HackerNewsConfig(fetch_top_stories=5, min_score=100)
    scraper = HackerNewsScraper(config, client)
    result = asyncio.run(scraper.fetch(_since()))
    assert result == []


def test_fetch_handles_topstories_http_error():
    client = _client_with_routes([], {}, topstories_error=True)
    config = HackerNewsConfig(fetch_top_stories=5)
    scraper = HackerNewsScraper(config, client)
    # Must not raise; returns [] when top-level httpx.HTTPError fires.
    result = asyncio.run(scraper.fetch(_since()))
    assert result == []


def test_fetch_skips_deleted_and_dead_comments():
    topstories = [301]
    # String keys required by _client_with_routes' ``items_by_id[str(story_id)]`` lookup.
    items = {
        "301": {"id": 301, "title": "T", "score": 200, "time": 1767312000,
                "by": "u", "kids": [11, 12, 13], "type": "story"},
        "11": {"id": 11, "text": "Alive", "by": "a"},
        "12": {"id": 12, "text": "Deleted", "by": "b", "deleted": True},
        "13": {"id": 13, "text": "Dead", "by": "c", "dead": True},
    }
    client = _client_with_routes(topstories, items)
    config = HackerNewsConfig(fetch_top_stories=5, min_score=0)
    scraper = HackerNewsScraper(config, client)
    result = asyncio.run(scraper.fetch(_since()))
    assert len(result) == 1
    content = result[0].content or ""
    assert "Alive" in content
    assert "Deleted" not in content
    assert "Dead" not in content
