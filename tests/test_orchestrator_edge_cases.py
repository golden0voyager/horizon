"""Edge-case tests for orchestrator dedup, proxy, and scraper dispatch."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models import (
    AIConfig,
    Config,
    ContentItem,
    FilteringConfig,
    OpenBBConfig,
    SourcesConfig,
    TwitterConfig,
)
from src.orchestrator import HorizonOrchestrator
from src.storage.manager import StorageManager


@pytest.fixture
def orchestrator() -> HorizonOrchestrator:
    cfg = Config(
        version="1.0",
        ai=AIConfig(provider="openai", model="x", api_key_env="OPENAI_API_KEY"),
        sources=SourcesConfig(),
        filtering=FilteringConfig(ai_score_threshold=7.0, time_window_hours=24),
    )
    return HorizonOrchestrator(cfg, MagicMock(spec=StorageManager))


def _item(**overrides: object) -> ContentItem:
    return ContentItem(
        id="test:x:https://e.com",
        source_type="rss",
        title="T",
        url="https://e.com",
        content="body",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        **overrides,
    )


# ---------------------------------------------------------------------------
# merge_topic_duplicates — edge cases
# ---------------------------------------------------------------------------


def test_merge_topic_duplicates_empty_duplicate_groups(
    orchestrator: HorizonOrchestrator,
) -> None:
    """When AI returns empty duplicates list, items pass through unchanged."""
    items = [_item(ai_score=9.0), _item(ai_score=7.0)]
    with patch("src.orchestrator.create_ai_client") as fake_create:
        fake_client = MagicMock()
        fake_client.complete = AsyncMock(return_value=json.dumps({"duplicates": []}))
        fake_create.return_value = fake_client
        out = asyncio.run(orchestrator.merge_topic_duplicates(items))
    assert out == items


def test_merge_topic_duplicates_invalid_group_skips(
    orchestrator: HorizonOrchestrator,
) -> None:
    """Non-list or short groups are silently skipped."""
    items = [_item(ai_score=9.0), _item(ai_score=7.0)]
    with patch("src.orchestrator.create_ai_client") as fake_create:
        fake_client = MagicMock()
        # Group is a string, not a list — should be skipped
        fake_client.complete = AsyncMock(return_value=json.dumps({"duplicates": ["not-a-list"]}))
        fake_create.return_value = fake_client
        out = asyncio.run(orchestrator.merge_topic_duplicates(items))
    assert out == items


def test_merge_topic_duplicates_out_of_bounds_primary_index(
    orchestrator: HorizonOrchestrator,
) -> None:
    """Primary index >= len(items) is skipped."""
    items = [_item(ai_score=9.0), _item(ai_score=7.0)]
    with patch("src.orchestrator.create_ai_client") as fake_create:
        fake_client = MagicMock()
        fake_client.complete = AsyncMock(return_value=json.dumps({"duplicates": [[99, 1]]}))
        fake_create.return_value = fake_client
        out = asyncio.run(orchestrator.merge_topic_duplicates(items))
    assert out == items


def test_merge_topic_duplicates_out_of_bounds_dup_index(
    orchestrator: HorizonOrchestrator,
) -> None:
    """Duplicate index >= len(items) is skipped."""
    items = [_item(ai_score=9.0), _item(ai_score=7.0)]
    with patch("src.orchestrator.create_ai_client") as fake_create:
        fake_client = MagicMock()
        fake_client.complete = AsyncMock(return_value=json.dumps({"duplicates": [[0, 99]]}))
        fake_create.return_value = fake_client
        out = asyncio.run(orchestrator.merge_topic_duplicates(items))
    assert out == items


def test_merge_topic_duplicates_same_index_skipped(
    orchestrator: HorizonOrchestrator,
) -> None:
    """Primary and duplicate pointing to same index is skipped."""
    items = [_item(ai_score=9.0), _item(ai_score=7.0)]
    with patch("src.orchestrator.create_ai_client") as fake_create:
        fake_client = MagicMock()
        fake_client.complete = AsyncMock(return_value=json.dumps({"duplicates": [[0, 0]]}))
        fake_create.return_value = fake_client
        out = asyncio.run(orchestrator.merge_topic_duplicates(items))
    assert out == items


# ---------------------------------------------------------------------------
# _expand_twitter_discussion — early returns
# ---------------------------------------------------------------------------


def test_expand_twitter_disabled_no_op(
    orchestrator: HorizonOrchestrator,
) -> None:
    """When Twitter config is disabled, expand returns immediately."""
    orchestrator.config.sources.twitter = TwitterConfig(enabled=False)
    asyncio.run(orchestrator._expand_twitter_discussion([_item()]))


def test_expand_twitter_fetch_reply_disabled_no_op(
    orchestrator: HorizonOrchestrator,
) -> None:
    """When fetch_reply_text is False, expand returns immediately."""
    orchestrator.config.sources.twitter = TwitterConfig(enabled=True, fetch_reply_text=False)
    asyncio.run(orchestrator._expand_twitter_discussion([_item()]))


# ---------------------------------------------------------------------------
# _enrich_important_items — empty list passthrough
# ---------------------------------------------------------------------------


def test_enrich_empty_list_noop(
    orchestrator: HorizonOrchestrator,
) -> None:
    """Empty items list returns immediately."""
    asyncio.run(orchestrator._enrich_important_items([]))


# ---------------------------------------------------------------------------
# _generate_summary — delegates correctly
# ---------------------------------------------------------------------------


def test_generate_summary_delegates(
    orchestrator: HorizonOrchestrator,
) -> None:
    """Summary generation calls DailySummarizer."""
    with patch("src.orchestrator.DailySummarizer") as fake_summarizer:
        fake = MagicMock()
        fake.generate_summary = AsyncMock(return_value="# Summary")
        fake_summarizer.return_value = fake
        result = asyncio.run(
            orchestrator._generate_summary([_item()], "2026-01-01", total_fetched=10)
        )
    assert result == "# Summary"
    fake.generate_summary.assert_awaited_once()


# ---------------------------------------------------------------------------
# _fetch_with_progress — sub-source breakdown printing
# ---------------------------------------------------------------------------


def test_fetch_with_progress_prints_sub_source_breakdown(
    orchestrator: HorizonOrchestrator,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When multiple sub-sources exist, _fetch_with_progress prints breakdown."""
    from datetime import datetime as dt

    items = [
        _item(metadata={"subreddit": "python"}),
        _item(metadata={"subreddit": "ai"}),
    ]
    scraper_mock = MagicMock()
    scraper_mock.fetch = AsyncMock(return_value=items)
    asyncio.run(orchestrator._fetch_with_progress("Reddit", scraper_mock, dt.now(UTC)))
    captured = capsys.readouterr()
    assert "r/python" in captured.out
    assert "r/ai" in captured.out


# ---------------------------------------------------------------------------
# Proxy branch in fetch_all_sources
# ---------------------------------------------------------------------------


def test_fetch_all_sources_uses_proxy_when_set(
    orchestrator: HorizonOrchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When PROXY env var is set, httpx.AsyncClient receives proxy kwarg."""
    monkeypatch.setenv("PROXY", "http://proxy.internal:8080")
    orchestrator.config.sources.hackernews.enabled = True

    with (
        patch.object(orchestrator, "_fetch_with_progress", new_callable=AsyncMock, return_value=[]),
        patch("httpx.AsyncClient") as fake_client,
    ):
        fake_ctx = MagicMock()
        fake_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
        fake_ctx.__aexit__ = AsyncMock(return_value=None)
        fake_client.return_value = fake_ctx
        asyncio.run(orchestrator.fetch_all_sources(datetime(2026, 1, 1, tzinfo=UTC)))
    call_kwargs = fake_client.call_args.kwargs
    assert call_kwargs.get("proxy") == "http://proxy.internal:8080"


# ---------------------------------------------------------------------------
# GitHub scraper instantiation
# ---------------------------------------------------------------------------


def test_fetch_all_sources_instantiates_github_scraper(
    orchestrator: HorizonOrchestrator,
) -> None:
    """When GitHub sources are configured, GitHubScraper is instantiated."""
    from src.models import GitHubSourceConfig

    orchestrator.config.sources.github = [GitHubSourceConfig(type="repo_releases", owner="test", repo="test")]
    with patch("src.orchestrator.GitHubScraper") as fake_github:
        fake_github.return_value.fetch = AsyncMock(return_value=[])
        asyncio.run(orchestrator.fetch_all_sources(datetime(2026, 1, 1, tzinfo=UTC)))
    fake_github.assert_called_once()


# ---------------------------------------------------------------------------
# OpenBB scraper instantiation
# ---------------------------------------------------------------------------


def test_fetch_all_sources_instantiates_openbb_scraper(
    orchestrator: HorizonOrchestrator,
) -> None:
    """When OpenBB is enabled, OpenBBScraper is instantiated."""
    orchestrator.config.sources.openbb = OpenBBConfig(enabled=True)
    with patch("src.orchestrator.OpenBBScraper") as fake_openbb:
        fake_openbb.return_value.fetch = AsyncMock(return_value=[])
        asyncio.run(orchestrator.fetch_all_sources(datetime(2026, 1, 1, tzinfo=UTC)))
    fake_openbb.assert_called_once()
