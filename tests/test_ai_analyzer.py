"""Tests for ``src/ai/analyzer.py`` — the AI content analyzer.

Coverage targets:
- ``_parse_json_response`` (delegates to ``src.ai.utils.parse_json_response``)
- ``_get_throttle_sec`` (clamp to ≥ 0)
- ``_get_concurrency`` (clamp to ≥ 1)
- ``analyze_batch`` (concurrency cap, throttle between items, error path → score=0,
  parse-failed response → score=0, Progress bar rendering)
- ``_analyze_item`` (content with/without Top Comments marker, all engagement
  metadata fields, score/reason/summary/tags population, parse failure fallback)

The ``tenacity`` retry decorator and the rich ``Progress`` bar are exercised
in their default paths only — the test environment never produces real
3-attempt failures since each ``_analyze_item`` is patched.
"""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

from src.ai.analyzer import ContentAnalyzer
from src.models import ContentItem, SourceType


def _make_item(
    item_id: str = "rss:test:1",
    *,
    title: str = "Interesting read",
    content: str | None = None,
    metadata: dict | None = None,
    author: str | None = "alice",
) -> ContentItem:
    return ContentItem(
        id=item_id,
        source_type=SourceType.RSS,
        title=title,
        url="https://example.com/post",
        content=content,
        author=author,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        metadata=metadata or {},
    )


class _FakeAIClient:
    """In-memory AIClient that returns canned JSON for ContentAnalyzer."""

    def __init__(self, **kwargs):
        # Mirror AIClient's shape so analyzer._get_concurrency/throttle
        # work via ``getattr(self.client, "config", ...)``.
        self.config = SimpleNamespace(throttle_sec=0.0, analysis_concurrency=1)
        for k, v in kwargs.items():
            setattr(self.config, k, v)
        self.responses = [
            {"score": 8.0, "reason": "high", "summary": "sub", "tags": ["a", "b"]}
        ]
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system, user, **kwargs):
        self.calls.append((system, user))
        # Pop the next canned response or return None-ish if exhausted.
        if self.responses:
            return json.dumps(self.responses.pop(0))
        return "{}"




# ---------------------------------------------------------------------------
# _parse_json_response
# ---------------------------------------------------------------------------


def test_parse_json_response_delegates_to_ai_utils():
    assert ContentAnalyzer._parse_json_response('{"a": 1}') == {"a": 1}
    assert ContentAnalyzer._parse_json_response("nope") is None


# ---------------------------------------------------------------------------
# _get_throttle_sec / _get_concurrency
# ---------------------------------------------------------------------------


def test_get_throttle_sec_clamps_negative_to_zero():
    client = _FakeAIClient(throttle_sec=-1.0)
    analyzer = ContentAnalyzer(client)
    assert analyzer._get_throttle_sec() == 0.0


def test_get_throttle_sec_returns_positive_value_unchanged():
    client = _FakeAIClient(throttle_sec=2.5)
    analyzer = ContentAnalyzer(client)
    assert analyzer._get_throttle_sec() == 2.5


def test_get_concurrency_clamps_below_one_to_one():
    client = _FakeAIClient(analysis_concurrency=0)
    analyzer = ContentAnalyzer(client)
    assert analyzer._get_concurrency() == 1


def test_get_concurrency_returns_positive_unchanged():
    client = _FakeAIClient(analysis_concurrency=4)
    analyzer = ContentAnalyzer(client)
    assert analyzer._get_concurrency() == 4


def test_get_throttle_sec_handles_missing_config_attribute():
    """A client with no ``config`` attribute → DEFAULT_THROTTLE_SEC = 0."""

    class _NoConfig:
        pass

    analyzer = ContentAnalyzer(_NoConfig())
    assert analyzer._get_throttle_sec() == 0.0
    assert analyzer._get_concurrency() == 1  # also uses default 1


# ---------------------------------------------------------------------------
# analyze_batch orchestration
# ---------------------------------------------------------------------------


def test_analyze_batch_does_not_sleep_when_throttle_zero(monkeypatch):
    """When ``throttle_sec=0``, no ``asyncio.sleep`` between items."""
    client = _FakeAIClient(throttle_sec=0.0)
    analyzer = ContentAnalyzer(client)
    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    async def fake_analyze(item):
        item.ai_score = 7.0

    monkeypatch.setattr(analyzer, "_analyze_item", fake_analyze)
    # rich.progress is hard to import in tests; mock it out entirely.
    monkeypatch.setattr("src.ai.analyzer.Progress", lambda *a, **kw: _NoopProgress())

    items = [_make_item(f"rss:t:{i}", title=f"t{i}") for i in range(3)]
    import src.ai.analyzer as analyzer_module
    monkeypatch.setattr(analyzer_module.asyncio, "sleep", fake_sleep)

    result = asyncio.run(analyzer.analyze_batch(items))

    assert sleep_calls == []
    assert len(result) == 3
    assert all(item.ai_score == 7.0 for item in result)


def test_analyze_batch_sleeps_between_items_when_throttle_configured(monkeypatch):
    """When ``throttle_sec>0``, sleep between consecutive items."""
    client = _FakeAIClient(throttle_sec=1.5)
    analyzer = ContentAnalyzer(client)
    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    async def fake_analyze(item):
        item.ai_score = 7.0

    monkeypatch.setattr(analyzer, "_analyze_item", fake_analyze)
    monkeypatch.setattr("src.ai.analyzer.Progress", lambda *a, **kw: _NoopProgress())

    items = [_make_item(f"rss:t:{i}", title=f"t{i}") for i in range(3)]
    import src.ai.analyzer as analyzer_module
    monkeypatch.setattr(analyzer_module.asyncio, "sleep", fake_sleep)

    asyncio.run(analyzer.analyze_batch(items))

    # 3 items → 2 inter-item sleeps, none for the last item.
    assert sleep_calls == [1.5, 1.5]


def test_analyze_batch_handles_exception_in_analyze_item(monkeypatch):
    """An exception inside ``_analyze_item`` sets score=0 and reason='Analysis failed'."""
    client = _FakeAIClient(analysis_concurrency=1)
    analyzer = ContentAnalyzer(client)

    async def failing_analyze(item):
        raise RuntimeError("upstream blew up")

    monkeypatch.setattr(analyzer, "_analyze_item", failing_analyze)
    monkeypatch.setattr("src.ai.analyzer.Progress", lambda *a, **kw: _NoopProgress())

    items = [_make_item("rss:t:fail", title="Bad news")]
    result = asyncio.run(analyzer.analyze_batch(items))

    assert len(result) == 1
    assert result[0].ai_score == 0.0
    assert result[0].ai_reason == "Analysis failed"


def test_analyze_batch_does_not_sleep_after_last_item(monkeypatch):
    """Throttle is suppressed after the LAST item, even with throttle_sec > 0."""
    client = _FakeAIClient(throttle_sec=0.5)
    analyzer = ContentAnalyzer(client)
    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    async def fake_analyze(item):
        item.ai_score = 5.0

    monkeypatch.setattr(analyzer, "_analyze_item", fake_analyze)
    monkeypatch.setattr("src.ai.analyzer.Progress", lambda *a, **kw: _NoopProgress())

    items = [_make_item("rss:single", title="only one")]
    import src.ai.analyzer as analyzer_module
    monkeypatch.setattr(analyzer_module.asyncio, "sleep", fake_sleep)

    asyncio.run(analyzer.analyze_batch(items))

    assert sleep_calls == []  # 1 item → 0 inter-item sleeps


def test_analyze_batch_concurrent_processing(monkeypatch):
    """Verify that higher concurrency allows overlapping item processing."""
    client = _FakeAIClient(analysis_concurrency=3)
    analyzer = ContentAnalyzer(client)
    items = [_make_item(f"rss:test:{i}") for i in range(5)]
    active_count = 0
    max_active = 0

    async def fake_analyze_item(item):
        nonlocal active_count, max_active
        active_count += 1
        max_active = max(max_active, active_count)
        await asyncio.sleep(0.05)
        active_count -= 1

    monkeypatch.setattr(analyzer, "_analyze_item", fake_analyze_item)
    monkeypatch.setattr("src.ai.analyzer.Progress", lambda *a, **kw: _NoopProgress())

    asyncio.run(analyzer.analyze_batch(items))

    assert max_active == 3
    assert all(item.ai_score is None for item in items)


def test_analyze_batch_concurrent_preserves_order(monkeypatch):
    """Verify that analyze_batch preserves input order in results."""
    client = _FakeAIClient(analysis_concurrency=3)
    analyzer = ContentAnalyzer(client)
    items = [_make_item(f"rss:test:{i}") for i in range(5)]

    async def fake_analyze_item(item):
        item.ai_score = float(item.id.split(":")[-1]) * 10

    monkeypatch.setattr(analyzer, "_analyze_item", fake_analyze_item)
    monkeypatch.setattr("src.ai.analyzer.Progress", lambda *a, **kw: _NoopProgress())

    result = asyncio.run(analyzer.analyze_batch(items))

    assert [item.id for item in result] == [item.id for item in items]


# ---------------------------------------------------------------------------
# _analyze_item
# ---------------------------------------------------------------------------


def test_analyze_item_populates_score_reason_summary_tags(monkeypatch):
    client = _FakeAIClient()
    analyzer = ContentAnalyzer(client)
    item = _make_item("rss:t:1", title="Hello", content="Some prose.")

    asyncio.run(analyzer._analyze_item(item))

    assert item.ai_score == 8.0
    assert item.ai_reason == "high"
    assert item.ai_summary == "sub"
    assert item.ai_tags == ["a", "b"]
    # The complete() call saw system=str (CONTENT_ANALYSIS_SYSTEM) + user contains
    # the title and source type.
    assert client.calls
    sys_prompt, user_prompt = client.calls[0]
    assert sys_prompt  # non-empty
    assert "Hello" in user_prompt
    assert "Some prose." in user_prompt


def test_analyze_item_separates_top_comments_section(monkeypatch):
    """When content contains ``--- Top Comments ---``, main + comments split correctly."""
    client = _FakeAIClient()
    analyzer = ContentAnalyzer(client)
    item = _make_item(
        item_id="rss:t:c",
        title="x",
        content="Main prose.\n\n--- Top Comments ---\nGreat post!\nI disagree.",
    )
    # monkeypatch the client to capture the user prompt for inspection.
    captured = {}

    async def _capture(system, user, **kw):
        captured["sys"] = system
        captured["user"] = user
        return json.dumps(
            {"score": 7, "reason": "r", "summary": "s", "tags": ["t"]}
        )

    client.complete = _capture
    asyncio.run(analyzer._analyze_item(item))

    assert "Main prose." in captured["user"]
    assert "Great post!" in captured["user"]
    # Discussion section also references comments.
    assert "Community Comments" in captured["user"]


def test_analyze_item_engagement_metadata_included(monkeypatch):
    client = _FakeAIClient()
    analyzer = ContentAnalyzer(client)
    item = _make_item(
        item_id="rss:t:engage",
        title="x",
        metadata={
            "score": 250,
            "descendants": 42,
            "favorite_count": 350,
            "retweet_count": 50,
            "reply_count": 12,
            "views": 9000,
            "bookmarks": 100,
            "upvote_ratio": 0.95,
        },
    )
    captured: dict = {}

    async def _capture(system, user, **kw):
        captured["user"] = user
        return json.dumps({"score": 6, "reason": "r", "summary": "s", "tags": ["t"]})

    client.complete = _capture
    asyncio.run(analyzer._analyze_item(item))

    user_prompt = captured["user"]
    for needle in (
        "score: 250",
        "42 comments",
        "350 likes",
        "50 retweets",
        "12 replies",
        "9000 views",
        "100 bookmarks",
        "upvote ratio: 95%",
    ):
        assert needle in user_prompt, needle


def test_analyze_item_handles_parse_failure(monkeypatch):
    """When AI returns unparseable text, default score=0 / reason='Analysis response parse failed'."""
    client = _FakeAIClient()
    analyzer = ContentAnalyzer(client)
    item = _make_item("rss:t:bad", title="Pencil")

    async def _garbage(system, user, **kw):
        return "this is not json at all"

    monkeypatch.setattr(client, "complete", _garbage)
    asyncio.run(analyzer._analyze_item(item))

    assert item.ai_score == 0.0
    assert item.ai_reason == "Analysis response parse failed"
    assert item.ai_summary == "Pencil"
    assert item.ai_tags == []


def test_analyze_item_uses_default_author_when_none(monkeypatch):
    client = _FakeAIClient()
    analyzer = ContentAnalyzer(client)
    item = _make_item("rss:t:anon", title="No author", author=None)
    captured: dict = {}

    async def _capture(system, user, **kw):
        captured["user"] = user
        return json.dumps({"score": 5, "reason": "r", "summary": "s", "tags": ["t"]})

    client.complete = _capture
    asyncio.run(analyzer._analyze_item(item))
    assert "Unknown" in captured["user"]


def test_analyze_item_handles_no_content(monkeypatch):
    """Content=None → prompt contains the unconditional "Content:" template header
    but the trailing body of the section is empty."""
    client = _FakeAIClient()
    analyzer = ContentAnalyzer(client)
    item = _make_item("rss:t:no-content", title="Stub", content=None)
    captured: dict = {}

    async def _capture(system, user, **kw):
        captured["user"] = user
        return json.dumps({"score": 4, "reason": "r", "summary": "s", "tags": ["t"]})

    client.complete = _capture
    asyncio.run(analyzer._analyze_item(item))

    # No discussion / engagement section generated (no content / no metadata keys).
    assert "Community Comments" not in captured["user"]
    assert "Engagement:" not in captured["user"]


def test_analyze_item_includes_discussion_url_when_present(monkeypatch):
    client = _FakeAIClient()
    analyzer = ContentAnalyzer(client)
    item = _make_item(
        "rss:t:disc",
        title="x",
        metadata={"discussion_url": "https://news.example/thread/1"},
    )
    captured: dict = {}

    async def _capture(system, user, **kw):
        captured["user"] = user
        return json.dumps({"score": 5, "reason": "r", "summary": "s", "tags": ["t"]})

    client.complete = _capture
    asyncio.run(analyzer._analyze_item(item))
    assert "https://news.example/thread/1" in captured["user"]


def test_analyze_item_includes_community_note(monkeypatch):
    client = _FakeAIClient()
    analyzer = ContentAnalyzer(client)
    item = _make_item(
        "rss:t:note", title="x",
        metadata={"community_note": "Context added by moderators"},
    )
    captured: dict = {}

    async def _capture(system, user, **kw):
        captured["user"] = user
        return json.dumps({"score": 5, "reason": "r", "summary": "s", "tags": ["t"]})

    client.complete = _capture
    asyncio.run(analyzer._analyze_item(item))
    assert "Context added by moderators" in captured["user"]


# ---------------------------------------------------------------------------
# helper: no-op Progress context manager for tests
# ---------------------------------------------------------------------------


class _NoopProgress:
    """Stand-in for ``rich.progress.Progress`` so tests don't pollute terminal output."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def add_task(self, *args, **kwargs):
        return 0

    def advance(self, *args, **kwargs):
        pass
