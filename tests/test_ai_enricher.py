"""Tests for ``src/ai/enricher.py`` — second-pass AI content enrichment.

Coverage targets:
- ``_parse_json_response`` (delegates to ``src.ai.utils.parse_json_response``)
- ``_get_concurrency`` (clamp to ≥ 1)
- ``enrich_batch`` (parallel fan-out via semaphore, exception → _translate_item fallback)
- ``_web_search`` (returns ``[]`` on exception, returns normalized shape)
- ``_extract_concepts`` (returns ``[]`` on parse failure, returns up to 3 queries)
- ``_enrich_item`` (happy path populates title_zh / detailed_summary_zh / background_zh /
  community_discussion_zh, parse failure falls back to translation, citation
  validation drops URLs not in web search results)
- ``_translate_item`` (populates title_zh + detailed_summary_zh on success,
  swallows exceptions so the item is never silently dropped)

Note: ``ContentEnricher._enrich_item`` is wrapped by ``tenacity``'s ``@retry``
(stop_after_attempt(3)). Tests rely on the FIRST attempt succeeding — the
fake AI client never raises an unexpected exception during enrichment,
and the ``_web_search`` monkeypatch uses ``async def`` so production's
``await self._web_search(query)`` line never blows up.
"""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.ai.enricher import ContentEnricher
from src.models import ContentItem, SourceType


def _make_item(
    item_id: str = "rss:t:1",
    *,
    title: str = "Some news",
    ai_score: float = 8.0,
    ai_reason: str = "high",
    ai_summary: str = "short",
    ai_tags: list[str] | None = None,
    content: str | None = "Some article body.",
    metadata: dict | None = None,
) -> ContentItem:
    """Build a ContentItem with AI fields set as instance attributes.

    ``ai_score`` / ``ai_summary`` / ``ai_tags`` / ``ai_reason`` live on the
    ``ContentItem`` dataclass but are NOT constructor kwargs in the model
    schema (they're attached post-construction by the analyzer). Tests
    set them directly on the instance so tests mirror runtime behaviour.
    """
    item = ContentItem(
        id=item_id,
        source_type=SourceType.RSS,
        title=title,
        url="https://example.com/post",
        content=content,
        author="alice",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        metadata=metadata or {"score": 50},
    )
    item.ai_score = ai_score
    item.ai_reason = ai_reason
    item.ai_summary = ai_summary
    item.ai_tags = ai_tags if ai_tags is not None else ["ai", "tech"]
    return item


class _FakeAIClient:
    """Stub AIClient with sequential canned responses for ContentEnricher.

    The enricher calls ``self.client.complete`` 2× per item (concept extraction,
    then enrichment), so the queue must have ≥ 2 entries per item.
    """

    def __init__(self, responses: list[str | Exception], **config_kwargs):
        self.responses = list(responses)
        self.call_count = 0
        self.config = SimpleNamespace(enrichment_concurrency=1, **config_kwargs)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system, user, **kwargs):
        self.calls.append((system, user))
        self.call_count += 1
        if not self.responses:
            return "{}"
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class _FakeWithConfig:
    """Client whose ``_get_concurrency`` reads ``enrichment_concurrency``."""

    def __init__(self, concurrency: int):
        self.config = SimpleNamespace(enrichment_concurrency=concurrency)


# ---------------------------------------------------------------------------
# _parse_json_response
# ---------------------------------------------------------------------------


def test_parse_json_response_delegates():
    assert ContentEnricher._parse_json_response('{"queries": ["q1"]}') == {"queries": ["q1"]}
    assert ContentEnricher._parse_json_response("not-json") is None


# ---------------------------------------------------------------------------
# _get_concurrency
# ---------------------------------------------------------------------------


def test_get_concurrency_clamps_below_one():
    enricher = ContentEnricher(_FakeWithConfig(concurrency=0))
    assert enricher._get_concurrency() == 1


def test_get_concurrency_returns_value():
    enricher = ContentEnricher(_FakeWithConfig(concurrency=4))
    assert enricher._get_concurrency() == 4


def test_get_concurrency_defaults_when_no_config():
    class _Client:
        pass

    enricher = ContentEnricher(_Client())
    assert enricher._get_concurrency() == 1


# ---------------------------------------------------------------------------
# enrich_batch orchestration
# ---------------------------------------------------------------------------


def test_enrich_batch_handles_single_item(monkeypatch):
    # 2 complete() calls per item (concepts + main enrichment)
    client = _FakeAIClient([
        json.dumps({"queries": []}),
        json.dumps({
            "title_zh": "测试标题", "title_en": "Test Title",
            "whats_new_en": "It happened",
            "background_en": "",
            "community_discussion_en": "",
        }),
    ])
    enricher = ContentEnricher(client)
    monkeypatch.setattr("src.ai.enricher.Progress", _noop_progress_context())

    item = _make_item()
    asyncio.run(enricher.enrich_batch([item]))

    assert item.metadata.get("title_zh") == "测试标题"
    assert item.metadata.get("title_en") == "Test Title"


def test_enrich_batch_exception_falls_back_to_translate(monkeypatch):
    """When ``_enrich_item`` raises, the exception handler must call ``_translate_item``."""
    client = _FakeAIClient([])
    enricher = ContentEnricher(client)

    calls = {"n": 0}

    async def failing_enrich(item):
        calls["n"] += 1
        raise RuntimeError("upstream down")

    async def fake_translate(item):
        item.metadata["title_zh"] = "[translated-fallback]"

    monkeypatch.setattr(enricher, "_enrich_item", failing_enrich)
    monkeypatch.setattr(enricher, "_translate_item", fake_translate)
    monkeypatch.setattr("src.ai.enricher.Progress", _noop_progress_context())

    item = _make_item()
    asyncio.run(enricher.enrich_batch([item]))

    assert calls["n"] == 1
    assert item.metadata["title_zh"] == "[translated-fallback]"


def test_enrich_batch_handles_multiple_items(monkeypatch):
    """Per-item responses are consumed in sequence; verify all items enriched."""
    n = 3
    client = _FakeAIClient([])
    # First two calls per item: concept extraction + enrichment
    for i in range(n):
        client.responses.append(json.dumps({"queries": []}))
        client.responses.append(json.dumps({"title_en": f"T{i}", "title_zh": f"标题{i}"}))

    enricher = ContentEnricher(client)
    monkeypatch.setattr("src.ai.enricher.Progress", _noop_progress_context())

    items = [_make_item(item_id=f"rss:t:{i}") for i in range(n)]
    asyncio.run(enricher.enrich_batch(items))

    for i, item in enumerate(items):
        assert item.metadata["title_en"] == f"T{i}"


# ---------------------------------------------------------------------------
# _web_search
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_query", ["", "   "])
def test_web_search_propagates_query_to_ddgs(monkeypatch, bad_query):
    """Verify DDGS.text receives the query string (smoke check)."""

    captured: dict = {}

    class _FakeDDGS:
        def text(self, query, max_results=3):
            captured["query"] = query
            captured["max_results"] = max_results
            return [{"title": "t", "href": "https://h", "body": "b"}]

    import src.ai.enricher as enricher_module
    monkeypatch.setattr(enricher_module, "DDGS", _FakeDDGS)

    enricher = ContentEnricher(_FakeAIClient([]))
    result = asyncio.run(enricher._web_search(bad_query))

    assert captured["query"] == bad_query
    assert result == [{"title": "t", "url": "https://h", "body": "b"}]


def test_web_search_returns_empty_when_ddgs_raises(monkeypatch):
    import src.ai.enricher as enricher_module

    class _BoomDDGS:
        def text(self, query, max_results=3):
            raise RuntimeError("DuckDuckGo unreachable")

    monkeypatch.setattr(enricher_module, "DDGS", _BoomDDGS)
    enricher = ContentEnricher(_FakeAIClient([]))

    result = asyncio.run(enricher._web_search("anything"))

    assert result == []


def test_web_search_normalizes_ddgs_response_shape(monkeypatch):
    import src.ai.enricher as enricher_module

    class _FakeDDGS:
        def text(self, query, max_results=3):
            return [
                {"title": "Web Title", "href": "https://web/1", "body": "Snippet 1"},
                {"title": "", "href": "", "body": ""},  # tolerated
            ]

    monkeypatch.setattr(enricher_module, "DDGS", _FakeDDGS)
    enricher = ContentEnricher(_FakeAIClient([]))

    result = asyncio.run(enricher._web_search("anything"))

    assert result == [
        {"title": "Web Title", "url": "https://web/1", "body": "Snippet 1"},
        {"title": "", "url": "", "body": ""},
    ]


# ---------------------------------------------------------------------------
# _extract_concepts
# ---------------------------------------------------------------------------


def test_extract_concepts_returns_queries_from_response():
    client = _FakeAIClient([json.dumps({"queries": ["q1", "q2", "q3", "q4"]})])
    # First 4 are returned; limit is 3.
    enricher = ContentEnricher(client)
    item = _make_item()

    result = asyncio.run(enricher._extract_concepts(item, "body text"))

    assert result == ["q1", "q2", "q3"]


def test_extract_concepts_returns_empty_on_parse_failure():
    client = _FakeAIClient(["not json at all"])
    enricher = ContentEnricher(client)
    item = _make_item()

    result = asyncio.run(enricher._extract_concepts(item, "body"))

    assert result == []


def test_extract_concepts_returns_empty_on_complete_failure():
    client = _FakeAIClient([RuntimeError("AI exploded")])
    enricher = ContentEnricher(client)
    item = _make_item()

    # Swallows exception → empty list (matches production semantics).
    result = asyncio.run(enricher._extract_concepts(item, "body"))

    assert result == []


def test_extract_concepts_returns_empty_when_response_has_no_queries():
    client = _FakeAIClient([json.dumps({"other_key": "value"})])
    enricher = ContentEnricher(client)
    item = _make_item()

    result = asyncio.run(enricher._extract_concepts(item, "body"))

    assert result == []


# ---------------------------------------------------------------------------
# _enrich_item — NOTE: web-search monkeypatches MUST be ``async def`` because
# production does ``await self._web_search(query)``. A sync lambda there would
# raise TypeError which tenacity would retry 3x and then re-raise, masking
# the real test failure as a TypeError.
# ---------------------------------------------------------------------------


def test_enrich_item_happy_path_populates_metadata(monkeypatch):
    """Complete concept extraction + enrichment: metadata receives structured fields."""
    search_results = [{"title": "Web", "url": "https://web/x", "body": "body"}]
    client = _FakeAIClient([
        json.dumps({"queries": ["x"]}),  # concept extraction
        json.dumps({
            "title_en": "T", "title_zh": "标题",
            "whats_new_en": "It happened",
            "whats_new_zh": "发生了",
            "why_it_matters_en": "It matters",
            "why_it_matters_zh": "有意义",
            "key_details_en": "v3",
            "key_details_zh": "v3",
            "background_en": "BG",
            "background_zh": "背景",
            "community_discussion_en": "CD",
            "community_discussion_zh": "社区",
            "sources": ["https://web/x"],
        }),
    ])
    enricher = ContentEnricher(client)

    async def _have_search_results(q, max_results=3):
        return search_results

    monkeypatch.setattr(enricher, "_web_search", _have_search_results)

    item = _make_item()
    asyncio.run(enricher._enrich_item(item))

    assert item.metadata["title_en"] == "T"
    assert item.metadata["title_zh"] == "标题"
    assert "It happened" in item.metadata["detailed_summary_en"]
    assert "发生了" in item.metadata["detailed_summary_zh"]
    assert item.metadata["background_en"] == "BG"
    assert item.metadata["background_zh"] == "背景"
    assert item.metadata["community_discussion_en"] == "CD"
    assert item.metadata["community_discussion_zh"] == "社区"
    assert item.metadata["sources"] == [{"url": "https://web/x", "title": "Web"}]
    # Backward-compat fallback fields populated.
    assert item.metadata["detailed_summary"] == item.metadata["detailed_summary_en"]


def test_enrich_item_drops_urls_not_in_available(monkeypatch):
    """Citation validation: only URLs that came from web search are kept."""
    search_results = [{"title": "Web1", "url": "https://web/known", "body": "b1"}]
    client = _FakeAIClient([
        json.dumps({"queries": ["x"]}),
        json.dumps({
            "sources": [
                "https://web/known",  # in available
                "https://web/HALLUCINATED",  # not in available — should be dropped
            ],
        }),
    ])
    enricher = ContentEnricher(client)

    async def _have_search_results(q, max_results=3):
        return search_results

    monkeypatch.setattr(enricher, "_web_search", _have_search_results)

    item = _make_item()
    asyncio.run(enricher._enrich_item(item))

    # Only the known URL survives; hallucinated one is dropped.
    assert item.metadata["sources"] == [
        {"url": "https://web/known", "title": "Web1"}
    ]


def test_enrich_item_drops_sources_when_no_available_urls(monkeypatch):
    """If web search returned nothing, the model may still cite URLs → drop all."""
    client = _FakeAIClient([
        json.dumps({"queries": ["x"]}),
        json.dumps({
            "sources": ["https://web/anything"],
        }),
    ])
    enricher = ContentEnricher(client)

    async def _empty_search(q, max_results=3):
        return []

    monkeypatch.setattr(enricher, "_web_search", _empty_search)

    item = _make_item()
    asyncio.run(enricher._enrich_item(item))

    assert "sources" not in item.metadata


def test_enrich_item_no_web_search_results_uses_placeholder_in_prompt(monkeypatch):
    """When ``queries=[]`` and web search returns nothing, ``web_context`` falls
    back to placeholder string ``\\"No web search results available.\\"``."""
    client_calls: list[tuple[str, str]] = []

    async def fake_complete(system, user, **kw):
        client_calls.append((system, user))
        # 1st call: concept extraction. Return ``{"queries": []}`` so queries stay empty.
        # 2nd call: enrichment. Return the title pair.
        if len(client_calls) == 1:
            return json.dumps({"queries": []})
        return json.dumps({"title_en": "T", "title_zh": "标题"})

    client = _FakeAIClient([])
    monkeypatch.setattr(client, "complete", fake_complete)

    enricher = ContentEnricher(client)

    async def _empty_search(q, max_results=3):
        return []

    monkeypatch.setattr(enricher, "_web_search", _empty_search)

    item = _make_item()
    asyncio.run(enricher._enrich_item(item))

    # Exactly 2 calls: concept extraction + main enrichment.
    assert len(client_calls) == 2
    _, user_prompt = client_calls[1]  # 2nd call = enrichment user prompt
    assert "No web search results available." in user_prompt


def test_enrich_item_parse_failure_falls_back_to_translate(monkeypatch):
    """When AI returns unparseable, _translate_item is called instead."""
    client = _FakeAIClient([
        json.dumps({"queries": ["x"]}),
        "totally not json",
    ])
    enricher = ContentEnricher(client)

    async def _empty_search(q, max_results=3):
        return []

    monkeypatch.setattr(enricher, "_web_search", _empty_search)

    calls = {"translated": False}

    async def fake_translate(item):
        calls["translated"] = True
        item.metadata["title_zh"] = "[t]"

    monkeypatch.setattr(enricher, "_translate_item", fake_translate)

    item = _make_item()
    asyncio.run(enricher._enrich_item(item))

    assert calls["translated"] is True
    assert item.metadata["title_zh"] == "[t]"


def test_enrich_item_accepts_string_values_for_metadata(monkeypatch):
    """Some field may come back as a dict OR a plain string. Both work."""
    client = _FakeAIClient([
        json.dumps({"queries": []}),
        json.dumps({
            # dict with text key
            "title_en": {"text": "From dict"},
            # plain string
            "title_zh": "Plain string",
            "whats_new_en": "",
            "whats_new_zh": "",
            "background_en": "",
            "background_zh": "",
            "community_discussion_en": "",
            "community_discussion_zh": "",
        }),
    ])
    enricher = ContentEnricher(client)

    async def _empty_search(q, max_results=3):
        return []

    monkeypatch.setattr(enricher, "_web_search", _empty_search)

    item = _make_item()
    asyncio.run(enricher._enrich_item(item))

    assert item.metadata["title_en"] == "From dict"
    assert item.metadata["title_zh"] == "Plain string"


# ---------------------------------------------------------------------------
# _translate_item
# ---------------------------------------------------------------------------


def test_translate_item_populates_title_and_summary():
    client = _FakeAIClient([
        json.dumps({"title_zh": "翻译标题", "summary_zh": "翻译摘要"})
    ])
    enricher = ContentEnricher(client)
    item = _make_item(ai_summary="Original summary.")

    asyncio.run(enricher._translate_item(item))

    assert item.metadata["title_zh"] == "翻译标题"
    assert item.metadata["detailed_summary_zh"] == "翻译摘要"


def test_translate_item_swallows_exception():
    """``_translate_item`` deliberately catches everything — never raises."""
    client = _FakeAIClient([RuntimeError("complete failed")])
    enricher = ContentEnricher(client)
    item = _make_item()

    # Must not raise.
    asyncio.run(enricher._translate_item(item))


def test_translate_item_handles_partial_response():
    """Only ``title_zh`` is present; ``summary_zh`` missing → only title populated."""
    client = _FakeAIClient([json.dumps({"title_zh": "Only this"})])
    enricher = ContentEnricher(client)
    item = _make_item()

    asyncio.run(enricher._translate_item(item))

    assert item.metadata["title_zh"] == "Only this"
    assert "detailed_summary_zh" not in item.metadata


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _async_return(value):
    async def _f(*args, **kwargs):
        return value

    return _f


def _noop_progress_context():
    """Builds a stand-in for ``rich.progress.Progress`` — already imported module."""

    class _NP:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def add_task(self, *a, **kw):
            return 0

        def advance(self, *a, **kw):
            pass

    def _factory(*a, **kw):
        return _NP()

    return _factory
