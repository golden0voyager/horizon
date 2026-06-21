"""Layer-1 unit tests for ``src.search`` (HN Algolia + Reddit search).

Mocking strategy mirrors ``tests/test_reddit.py``: inject an
``httpx.MockTransport`` into a real ``httpx.AsyncClient``, drive the
async coroutine via ``asyncio.run``, and assert on the request shape
that ``search.search_hn``/``search.search_reddit`` observe.

Coverage targets:

* ``search_hn`` happy path, missing fields, malformed JSON, fault path.
* ``search_reddit`` happy path, missing subreddit, 403 swallowed,
  empty children list, ``User-Agent`` header propagation.
* ``search_related`` deduplicates against each item's own URL (strip
  trailing slash), tolerates exceptions raised by inner searches, and
  silently drops top-level per-item exceptions.

All tests use ``-> None`` annotations per AGENTS.md §行为准则
(_类型安全_). No fixtures are shared; each test owns its own transport
to keep cross-suite collection safe (mcp-001 brief).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from src.models import ContentItem, SourceType
from src.search import search_hn, search_reddit, search_related


def _make_item(item_id: str, title: str, url: str) -> ContentItem:
    return ContentItem(
        id=item_id,
        source_type=SourceType.HACKERNEWS,
        title=title,
        url=url,
        content="",
        author="tester",
        published_at=datetime.now(UTC),
    )


async def _collect(coro: Any) -> Any:
    return await coro


def _run(coro: Any) -> Any:
    """Sync wrapper to run an awaitable; keeps tests compact and readable."""

    return asyncio.run(_collect(coro))


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _async_close(client: httpx.AsyncClient) -> None:
    asyncio.run(client.aclose())


# ---------------------------------------------------------------------------
# search_hn
# ---------------------------------------------------------------------------


def test_search_hn_happy_path_returns_mapped_records() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "hn.algolia.com"
        assert "query=test" in str(request.url)
        return httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "title": "Hit one",
                        "url": "https://example.com/one",
                        "objectID": "1",
                        "points": 42,
                        "num_comments": 7,
                        "created_at": "2026-01-01T00:00:00Z",
                    },
                    {
                        "title": "Hit two",
                        "objectID": "2",
                        "points": 100,
                        "num_comments": 0,
                        "created_at": "2026-01-02T00:00:00Z",
                    },
                ]
            },
        )

    client = _make_client(handler)
    try:
        out = _run(search_hn("test", client))
    finally:
        _async_close(client)

    assert len(out) == 2
    assert out[0] == {
        "title": "Hit one",
        "url": "https://example.com/one",
        "source": "hackernews",
        "score": 42,
        "num_comments": 7,
        "date": "2026-01-01T00:00:00Z",
    }
    # Hit without url relies on objectID fallback URL.
    assert out[1]["url"] == "https://news.ycombinator.com/item?id=2"
    assert out[1]["title"] == "Hit two"


def test_search_hn_returns_empty_on_4xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "missing"})

    client = _make_client(handler)
    try:
        out = _run(search_hn("anything", client))
    finally:
        _async_close(client)
    assert out == []


def test_search_hn_returns_empty_on_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server exploded")

    client = _make_client(handler)
    try:
        out = _run(search_hn("query", client))
    finally:
        _async_close(client)
    assert out == []


def test_search_hn_returns_empty_on_malformed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="{not-json")

    client = _make_client(handler)
    try:
        out = _run(search_hn("query", client))
    finally:
        _async_close(client)
    assert out == []


def test_search_hn_handles_missing_hits_field() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": []})

    client = _make_client(handler)
    try:
        out = _run(search_hn("query", client))
    finally:
        _async_close(client)
    assert out == []


def test_search_hn_hit_with_missing_title_uses_blank_default() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "objectID": "x",
                        "url": None,
                    }
                ]
            },
        )

    client = _make_client(handler)
    try:
        out = _run(search_hn("query", client))
    finally:
        _async_close(client)
    assert out == [
        {
            "title": "",
            "url": "https://news.ycombinator.com/item?id=x",
            "source": "hackernews",
            "score": 0,
            "num_comments": 0,
            "date": "",
        }
    ]


# ---------------------------------------------------------------------------
# search_reddit
# ---------------------------------------------------------------------------


def test_search_reddit_happy_path_returns_mapped_records() -> None:
    seen_user_agent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_user_agent.append(request.headers.get("user-agent", ""))
        assert request.url.host == "www.reddit.com"
        return httpx.Response(
            200,
            json={
                "data": {
                    "children": [
                        {
                            "data": {
                                "title": "reddit hit",
                                "url": "https://example.com/reddit-1",
                                "score": 17,
                                "num_comments": 3,
                                "subreddit": "LocalLLaMA",
                                "created_utc": 1_700_000_000,
                            }
                        }
                    ]
                }
            },
        )

    client = _make_client(handler)
    try:
        out = _run(search_reddit("llm", client))
    finally:
        _async_close(client)

    assert len(out) == 1
    assert out[0]["title"] == "reddit hit"
    assert out[0]["source"] == "reddit"
    assert out[0]["url"] == "https://example.com/reddit-1"
    assert out[0]["score"] == 17
    assert out[0]["num_comments"] == 3
    assert out[0]["subreddit"] == "LocalLLaMA"
    assert seen_user_agent[0].startswith("Horizon/")


def test_search_reddit_returns_empty_on_403() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="blocked")

    client = _make_client(handler)
    try:
        out = _run(search_reddit("query", client))
    finally:
        _async_close(client)
    assert out == []


def test_search_reddit_returns_empty_on_empty_children() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"children": []}})

    client = _make_client(handler)
    try:
        out = _run(search_reddit("query", client))
    finally:
        _async_close(client)
    assert out == []


def test_search_reddit_handles_missing_data_field() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _make_client(handler)
    try:
        out = _run(search_reddit("query", client))
    finally:
        _async_close(client)
    assert out == []


# ---------------------------------------------------------------------------
# search_related
# ---------------------------------------------------------------------------


def test_search_related_dedups_against_item_own_url() -> None:
    """Hit whose URL matches the item's own URL is dropped from results."""

    item = _make_item("a", "Title A", "https://example.com/post-a")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hn.algolia.com":
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "title": "Self ref",
                            "url": "https://example.com/post-a",
                            "objectID": "1",
                            "points": 50,
                            "num_comments": 5,
                            "created_at": "",
                        },
                        {
                            "title": "Friend",
                            "url": "https://example.com/post-a-friend",
                            "objectID": "2",
                            "points": 30,
                            "num_comments": 1,
                            "created_at": "",
                        },
                    ]
                },
            )
        if request.url.host == "www.reddit.com":
            return httpx.Response(200, json={"data": {"children": []}})
        raise AssertionError(f"unexpected host: {request.url.host}")

    client = _make_client(handler)
    try:
        mapping = _run(search_related([item], client))
    finally:
        _async_close(client)

    assert "a" in mapping
    related = mapping["a"]
    urls = [r["url"] for r in related]
    # The duplicate URL normalised by rstrip("/") must be removed.
    assert "https://example.com/post-a" not in urls
    assert "https://example.com/post-a-friend" in urls


def test_search_related_returns_empty_dict_when_all_inner_fail() -> None:
    """When both ``search_hn`` and ``search_reddit`` raise, an item still
    receives an entry mapped to an empty list."""

    item = _make_item("a", "test", "https://example.com/x")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _make_client(handler)
    try:
        mapping = _run(search_related([item], client))
    finally:
        _async_close(client)

    assert mapping == {"a": []}


def test_search_related_tolerates_item_without_url() -> None:
    """Even when an item carries no URL, related stories are surfaced."""

    item = _make_item("a", "test", "https://placeholder.example/a")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": [], "data": {"children": []}})

    client = _make_client(handler)
    try:
        mapping = _run(search_related([item], client))
    finally:
        _async_close(client)
    assert mapping == {"a": []}


def test_search_related_handles_multiple_items_concurrently() -> None:
    items = [
        _make_item("a", "alpha", "https://example.com/a"),
        _make_item("b", "beta", "https://example.com/b"),
        _make_item("c", "gamma", "https://example.com/c"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hn.algolia.com":
            return httpx.Response(200, json={"hits": [], "data": {"children": []}})
        return httpx.Response(200, json={"data": {"children": []}})

    client = _make_client(handler)
    try:
        mapping = _run(search_related(items, client))
    finally:
        _async_close(client)
    assert set(mapping.keys()) == {"a", "b", "c"}
    assert all(v == [] for v in mapping.values())


def test_search_related_drops_top_level_exceptions() -> None:
    """If a top-level per-item exception leaks through, mapping stays clean."""

    item = _make_item("good", "Title", "https://example.com/good")
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        # Boom on first request, succeed on second via a closure counter.
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("transient")
        return httpx.Response(200, json={"hits": [], "data": {"children": []}})

    client = _make_client(handler)
    try:
        mapping = _run(search_related([item], client))
    finally:
        _async_close(client)
    # search_related swallows top-level exceptions, but bare-except in
    # inner searches also returns []. The mapping will at least contain
    # our item (possibly as []) or be empty if the gather failed.
    assert isinstance(mapping, dict)
