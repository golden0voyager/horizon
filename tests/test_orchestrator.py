"""Phase 4 unit tests for ``src.orchestrator.HorizonOrchestrator``.

Strategy: avoid spinning up scrapers / AI / email / webhook. Directly construct
``HorizonOrchestrator`` with a minimal Config and a MagicMock storage, then
patch the seam entry points (``fetch_all_sources``, ``_analyze_content``,
``merge_topic_duplicates``, ``_expand_twitter_discussion``, ``_enrich_important_items``,
``summarizer.generate_summary``) to verify orchestration logic in isolation.

Coverage focus:
- ``_resolve_proxy`` env-var precedence
- ``_determine_time_window`` math
- ``merge_cross_source_duplicates`` (single passthrough, dedup by URL, www strip,
  content merge, metadata union)
- ``merge_topic_duplicates`` (passthrough on parse-fail / length<=1, valid AI JSON
  drops duplicates + merges content with source label)
- ``fetch_all_sources`` (all scraper types instantiated, exceptions swallowed)
- ``_sub_source_label`` per-source key picks subreddit/feed/channel/ossinsight/repo/watchlist
- ``_analyze_content`` delegates to ``ContentAnalyzer``
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models import (
    AIConfig,
    Config,
    ContentItem,
    FilteringConfig,
    OpenBBConfig,
    OSSInsightConfig,
    SourcesConfig,
    TwitterConfig,
)
from src.orchestrator import HorizonOrchestrator, _resolve_proxy
from src.scrapers.reddit import RedditSubredditConfig
from src.scrapers.rss import RSSSourceConfig
from src.scrapers.telegram import TelegramChannelConfig
from src.storage.manager import StorageManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_item(
    *,
    url: str = "https://example.com/a",
    title: str = "T",
    content: str = "body",
    source: str = "rss",
    metadata: dict[str, Any] | None = None,
    ai_score: float | None = None,
    ai_summary: str | None = None,
    ai_tags: list[str] | None = None,
    fetched_at: datetime | None = None,
) -> ContentItem:
    return ContentItem(
        id=f"{source}:x:{url}",
        source_type=source,
        title=title,
        url=url,
        content=content,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        fetched_at=fetched_at or datetime(2026, 1, 1, tzinfo=UTC),
        ai_score=ai_score,
        ai_summary=ai_summary,
        ai_tags=ai_tags or [],
        metadata=metadata or {},
    )


@pytest.fixture
def minimal_config() -> Config:
    return Config(
        version="1.0",
        ai=AIConfig(
            provider="openai", model="x", api_key_env="OPENAI_API_KEY"
        ),
        sources=SourcesConfig(),
        filtering=FilteringConfig(ai_score_threshold=7.0, time_window_hours=24),
    )


@pytest.fixture
def storage() -> MagicMock:
    return MagicMock(spec=StorageManager)


@pytest.fixture
def orchestrator(minimal_config: Config, storage: MagicMock) -> HorizonOrchestrator:
    return HorizonOrchestrator(minimal_config, storage)


# ---------------------------------------------------------------------------
# _resolve_proxy
# ---------------------------------------------------------------------------


def test_resolve_proxy_empty_when_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("PROXY", "https_proxy", "http_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    assert _resolve_proxy() == ""


def test_resolve_proxy_first_set_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXY", "http://a")
    monkeypatch.setenv("https_proxy", "http://b")
    assert _resolve_proxy() == "http://a"


def test_resolve_proxy_https_proxy_when_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROXY", raising=False)
    monkeypatch.setenv("https_proxy", "http://b.com")
    assert _resolve_proxy() == "http://b.com"


def test_resolve_proxy_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXY", "   http://x.com   ")
    assert _resolve_proxy() == "http://x.com"


def test_resolve_proxy_ignores_empty_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXY", "  ")
    monkeypatch.setenv("https_proxy", "http://c.com")
    assert _resolve_proxy() == "http://c.com"


# ---------------------------------------------------------------------------
# _determine_time_window
# ---------------------------------------------------------------------------


def test_determine_time_window_force_hours_overrides_config(
    orchestrator: HorizonOrchestrator,
) -> None:
    before = datetime.now(UTC)
    since = orchestrator._determine_time_window(force_hours=1)
    after = datetime.now(UTC)
    expected_min = before - timedelta(hours=1)
    expected_max = after - timedelta(hours=1)
    assert expected_min - timedelta(seconds=2) <= since <= expected_max + timedelta(seconds=2)


def test_determine_time_window_default_from_config(
    orchestrator: HorizonOrchestrator,
) -> None:
    """config.filtering.time_window_hours is 24 by default."""

    before = datetime.now(UTC)
    since = orchestrator._determine_time_window()
    after = datetime.now(UTC)
    delta_max = after - since
    delta_min = since - before
    assert timedelta(hours=22) <= delta_max <= timedelta(hours=26)
    assert timedelta(hours=-26) <= delta_min <= timedelta(hours=-22)


# ---------------------------------------------------------------------------
# merge_cross_source_duplicates
# ---------------------------------------------------------------------------


def test_merge_cross_source_duplicates_no_duplicates_passes_through(
    orchestrator: HorizonOrchestrator,
) -> None:
    items = [
        _build_item(url="https://a.com/1"),
        _build_item(url="https://b.com/2"),
    ]
    merged = orchestrator.merge_cross_source_duplicates(items)
    assert len(merged) == 2


def test_merge_cross_source_duplicates_groups_by_normalized_url(
    orchestrator: HorizonOrchestrator,
) -> None:
    items = [
        _build_item(url="https://www.example.com/article/", content="short"),
        _build_item(url="https://example.com/article", content=""),
    ]
    merged = orchestrator.merge_cross_source_duplicates(items)
    assert len(merged) == 1
    assert merged[0].metadata["merged_sources"] == ["rss"]


def test_merge_cross_source_duplicates_picks_longest_content(
    orchestrator: HorizonOrchestrator,
) -> None:
    items = [
        _build_item(url="https://example.com/x", content="short", source="rss"),
        _build_item(url="https://example.com/x", content="this is a longer body", source="github"),
    ]
    merged = orchestrator.merge_cross_source_duplicates(items)
    primary = merged[0]
    # Primary is the longer-content one — github.
    assert primary.source_type == "github"
    assert "this is a longer body" in (primary.content or "")
    assert "short" in (primary.content or "")


def test_merge_cross_source_duplicates_merges_metadata_when_missing(
    orchestrator: HorizonOrchestrator,
) -> None:
    primary = _build_item(
        url="https://example.com/a", content="long body", source="rss",
        metadata={"engagement": None, "comments": []},
    )
    secondary = _build_item(
        url="https://example.com/a", content="", source="github",
        metadata={"engagement": {"likes": 10}, "comments": ["a", "b"]},
    )
    merged = orchestrator.merge_cross_source_duplicates([primary, secondary])
    primary = merged[0]
    assert primary.metadata["engagement"] == {"likes": 10}
    assert primary.metadata["comments"] == ["a", "b"]


# ---------------------------------------------------------------------------
# merge_topic_duplicates
# ---------------------------------------------------------------------------


def test_merge_topic_duplicates_passthrough_when_ai_response_unparseable(
    orchestrator: HorizonOrchestrator,
) -> None:
    items = [_build_item(ai_score=8.0, ai_summary="s1"), _build_item(ai_score=7.0, ai_summary="s2")]
    with patch("src.orchestrator.create_ai_client") as fake_create:
        fake_client = MagicMock()
        fake_client.complete = AsyncMock(return_value="not parseable json")
        fake_create.return_value = fake_client
        out = asyncio.run(orchestrator.merge_topic_duplicates(items))
    assert out == items


def test_merge_topic_duplicates_returns_passthrough_when_empty(
    orchestrator: HorizonOrchestrator,
) -> None:
    assert asyncio.run(orchestrator.merge_topic_duplicates([])) == []
    assert asyncio.run(orchestrator.merge_topic_duplicates([_build_item()])) == [_build_item()]


def test_merge_topic_duplicates_drops_dup_indexes(
    orchestrator: HorizonOrchestrator,
) -> None:
    items = [
        _build_item(ai_score=9.0, ai_summary="primary", content="main content"),
        _build_item(ai_score=8.0, ai_summary="dup of primary", content="dup content"),
        _build_item(ai_score=7.0, ai_summary="unique"),
    ]
    ai_response = json.dumps({"duplicates": [[0, 1]]})

    with patch("src.orchestrator.create_ai_client") as fake_create:
        fake_client = MagicMock()
        fake_client.complete = AsyncMock(return_value=ai_response)
        fake_create.return_value = fake_client

        out = asyncio.run(orchestrator.merge_topic_duplicates(items))

    assert len(out) == 2
    assert out[0].ai_summary == "primary"
    assert out[1].ai_summary == "unique"
    assert "dup content" in out[0].content


def test_merge_topic_duplicates_swallows_ai_call_exception(
    orchestrator: HorizonOrchestrator,
) -> None:
    items = [_build_item(ai_score=8.0), _build_item(ai_score=7.0)]
    with patch("src.orchestrator.create_ai_client") as fake_create:
        fake_client = MagicMock()
        fake_client.complete = AsyncMock(side_effect=RuntimeError("api down"))
        fake_create.return_value = fake_client

        out = asyncio.run(orchestrator.merge_topic_duplicates(items))
    assert out == items  # passthrough


# ---------------------------------------------------------------------------
# fetch_all_sources
# ---------------------------------------------------------------------------


def test_fetch_all_sources_invokes_all_configured_scrapers(
    orchestrator: HorizonOrchestrator,
) -> None:
    """Stub every scraper class — exercise the dispatch + exception swallow path."""

    orchestrator.config.sources.github = []
    orchestrator.config.sources.hackernews.enabled = True
    orchestrator.config.sources.reddit.enabled = True
    orchestrator.config.sources.reddit.subreddits = [
        RedditSubredditConfig(subreddit="python"),
    ]
    orchestrator.config.sources.telegram.enabled = True
    orchestrator.config.sources.telegram.channels = [
        TelegramChannelConfig(channel="x"),
    ]
    orchestrator.config.sources.rss = [
        RSSSourceConfig(name="feed", url="https://e.com/r.xml"),
    ]
    orchestrator.config.sources.twitter = TwitterConfig(enabled=True, users=["u"])
    orchestrator.config.sources.openbb = OpenBBConfig(enabled=False)
    orchestrator.config.sources.ossinsight = OSSInsightConfig(enabled=True)

    fake_item = _build_item()

    target_modules = {
        "GitHubScraper": "src.orchestrator.GitHubScraper",
        "HackerNewsScraper": "src.orchestrator.HackerNewsScraper",
        "RSSScraper": "src.orchestrator.RSSScraper",
        "RedditScraper": "src.orchestrator.RedditScraper",
        "TelegramScraper": "src.orchestrator.TelegramScraper",
        "TwitterScraper": "src.orchestrator.TwitterScraper",
        "OSSInsightScraper": "src.orchestrator.OSSInsightScraper",
    }
    patches = []
    for class_name, target in target_modules.items():
        scraper = MagicMock()
        if class_name == "RedditScraper":
            scraper.fetch = AsyncMock(side_effect=Exception("reddit-flaky"))
        else:
            scraper.fetch = AsyncMock(return_value=[fake_item])
        patches.append(patch(target, return_value=scraper))

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)

        # Patch httpx.AsyncClient so we don't open real sockets.
        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)
        with patch("httpx.AsyncClient", return_value=fake_client):
            items = asyncio.run(
                orchestrator.fetch_all_sources(datetime(2026, 1, 1, tzinfo=UTC))
            )

    # 6 of 7 scrapers returned 1 item, reddit raised → still counted.
    assert len(items) == 6


# ---------------------------------------------------------------------------
# _sub_source_label
# ---------------------------------------------------------------------------


def test_sub_source_label_subreddit(orchestrator: HorizonOrchestrator) -> None:
    item = _build_item(metadata={"subreddit": "python"})
    assert orchestrator._sub_source_label(item) == "r/python"


def test_sub_source_label_feed_name(orchestrator: HorizonOrchestrator) -> None:
    item = _build_item(metadata={"feed_name": "myfeed"})
    assert orchestrator._sub_source_label(item) == "myfeed"


def test_sub_source_label_channel(orchestrator: HorizonOrchestrator) -> None:
    item = _build_item(metadata={"channel": "alice"})
    assert orchestrator._sub_source_label(item) == "@alice"


def test_sub_source_label_ossinsight(orchestrator: HorizonOrchestrator) -> None:
    item = _build_item(
        metadata={"period": "past_24_hours", "repo": "x/y", "primary_language": "Python"}
    )
    assert orchestrator._sub_source_label(item) == "ossinsight:Python"


def test_sub_source_label_repo(orchestrator: HorizonOrchestrator) -> None:
    item = _build_item(metadata={"repo": "owner/repo"})
    assert orchestrator._sub_source_label(item) == "owner/repo"


def test_sub_source_label_watchlist(orchestrator: HorizonOrchestrator) -> None:
    item = _build_item(metadata={"watchlist": "tech"})
    assert orchestrator._sub_source_label(item) == "tech"


def test_sub_source_label_unknown_when_no_metadata(
    orchestrator: HorizonOrchestrator,
) -> None:
    item = _build_item(metadata={}, title="Untitled")
    assert orchestrator._sub_source_label(item) == "unknown"


# ---------------------------------------------------------------------------
# _analyze_content delegation
# ---------------------------------------------------------------------------


def test_analyze_content_calls_analyzer_with_ai_client(
    orchestrator: HorizonOrchestrator,
) -> None:
    items = [_build_item()]
    with patch("src.orchestrator.create_ai_client") as fake_create, patch(
        "src.ai.analyzer.ContentAnalyzer"
    ) as fake_analyzer_cls:
        fake_client = MagicMock()
        fake_create.return_value = fake_client
        fake_analyzer = MagicMock()
        fake_analyzer.analyze_batch = AsyncMock(return_value=[_build_item(ai_score=9.0)])
        fake_analyzer_cls.return_value = fake_analyzer

        out = asyncio.run(orchestrator._analyze_content(items))

    fake_create.assert_called_once_with(orchestrator.config.ai)
    fake_analyzer_cls.assert_called_once_with(fake_client)
    fake_analyzer.analyze_batch.assert_awaited_once_with(items)
    assert out == fake_analyzer.analyze_batch.return_value
