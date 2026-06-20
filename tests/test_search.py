"""Tests for ``src/search.py`` — HN Algolia + Reddit JSON search helpers.

Covers:
- ``search_hn`` happy path (parses hits into the documented schema).
- ``search_hn`` fallback to objectID-based URL when ``url`` is missing.
- ``search_hn`` on HTTP error → returns ``[]`` (no raise).
- ``search_reddit`` happy path (parses data.children[*].data into the schema).
- ``search_reddit`` on HTTP error → returns ``[]``.
- ``search_related`` concurrent fan-out via ``asyncio.gather``.
- ``search_related`` deduplicates results against the source item's URL.
- ``search_related`` swallows exceptions from either backend.
- ``search_related`` returns ``{}`` for empty input list.

httpx ``MockTransport`` is the same pattern used in ``tests/test_twitter.py``
reply tests — no real network calls.
"""

import asyncio
from datetime import UTC, datetime

import httpx

from src.models import ContentItem, SourceType
from src.search import search_hn, search_reddit, search_related


def _item(url: str = "https://blog.example/post-1", title: str = "Interesting read") -> ContentItem:
    return ContentItem(
        id="rss:test:1",
        source_type=SourceType.RSS,
        title=title,
        url=url,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# search_hn
# ---------------------------------------------------------------------------


def test_search_hn_returns_normalized_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "hits": [
                {
                    "title": "Story A",
                    "url": "https://example.com/a",
                    "objectID": "1",
                    "points": 100,
                    "num_comments": 12,
                    "created_at": "2025-01-01T00:00:00+00:00",
                },
                {
                    "title": "Story B (no url)",
                    "url": None,
                    "objectID": "2",
                    "points": 50,
                    "num_comments": 0,
                    "created_at": "2025-01-02T00:00:00+00:00",
                },
            ]
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(search_hn("anything", client))
    asyncio.run(client.aclose())

    assert len(result) == 2
    assert result[0]["title"] == "Story A"
    assert result[0]["source"] == "hackernews"
    assert result[0]["score"] == 100
    assert result[1]["url"].startswith("https://news.ycombinator.com/item?id=")


def test_search_hn_returns_empty_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "upstream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(search_hn("query", client))
    asyncio.run(client.aclose())
    assert result == []


# ---------------------------------------------------------------------------
# search_reddit
# ---------------------------------------------------------------------------


def test_search_reddit_returns_normalized_results():
    def handler(request: httpx.Request) -> httpx.Response:
        # Verify reasonable headers are sent (User-Agent).
        return httpx.Response(200, json={
            "data": {
                "children": [
                    {
                        "data": {
                            "title": "Reddit post A",
                            "url": "https://reddit.com/r/x/comments/1",
                            "score": 200,
                            "num_comments": 30,
                            "subreddit": "Python",
                            "created_utc": 1700000000,
                        }
                    },
                ]
            }
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(search_reddit("anything", client))
    asyncio.run(client.aclose())

    assert len(result) == 1
    assert result[0]["source"] == "reddit"
    assert result[0]["title"] == "Reddit post A"
    assert result[0]["subreddit"] == "Python"


def test_search_reddit_returns_empty_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(search_reddit("query", client))
    asyncio.run(client.aclose())
    assert result == []


# ---------------------------------------------------------------------------
# search_related — orchestration
# ---------------------------------------------------------------------------


def test_search_related_returns_empty_dict_for_no_items():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    result = asyncio.run(search_related([], client))
    asyncio.run(client.aclose())
    assert result == {}


def test_search_related_concurrent_fanout_merges_hn_and_reddit():
    """For each item, run search_hn + search_reddit concurrently and merge results."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "algolia.com" in request.url.host:
            return httpx.Response(200, json={"hits": [
                {"title": "HN matched", "url": "https://hn.example/a", "objectID": "1",
                 "points": 10, "num_comments": 1, "created_at": "2025-01-01T00:00:00+00:00"}
            ]})
        return httpx.Response(200, json={"data": {"children": [
            {"data": {"title": "Reddit matched", "url": "https://reddit.com/r/x/c/1",
                      "score": 5, "num_comments": 2, "subreddit": "x",
                      "created_utc": 1700000000}}
        ]}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    item = _item(title="my-search-query", url="https://blog.example/post-1")
    result = asyncio.run(search_related([item], client))
    asyncio.run(client.aclose())

    assert item.id in result
    matched = result[item.id]
    sources = {m["source"] for m in matched}
    assert sources == {"hackernews", "reddit"}


def test_search_related_dedupes_against_item_url():
    def handler(request: httpx.Request) -> httpx.Response:
        if "algolia.com" in request.url.host:
            return httpx.Response(200, json={"hits": [
                {"title": "Same URL as item", "url": "https://blog.example/post-1",
                 "objectID": "1", "points": 10, "num_comments": 1,
                 "created_at": "2025-01-01T00:00:00+00:00"}
            ]})
        return httpx.Response(200, json={"data": {"children": []}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    item = _item(url="https://blog.example/post-1")
    result = asyncio.run(search_related([item], client))
    asyncio.run(client.aclose())
    assert result[item.id] == []


def test_search_related_treats_backend_exceptions_as_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    item = _item()
    result = asyncio.run(search_related([item], client))
    asyncio.run(client.aclose())
    # Both backends returned errors → empty related list for that item.
    assert result[item.id] == []


def test_search_related_handles_multiple_items():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    items = [_item(title=f"t{i}", url=f"https://blog.example/post-{i}") for i in range(3)]
    result = asyncio.run(search_related(items, client))
    asyncio.run(client.aclose())
    assert set(result.keys()) == {item.id for item in items}
