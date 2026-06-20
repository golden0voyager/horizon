"""Phase 6 unit tests for ``src.setup.wizard`` — pure-Python helpers.

The interactive prompt-driven ``configure_ai`` / ``get_interests`` /
``select_sources`` / ``main`` cannot be unit-tested without mocking rich /
stdin. We focus on the deterministic assembly helpers:

- ``build_config``: ``selected_sources`` list → valid ``Config``.
- ``merge_configs``: dedup by URL/subreddit key, preserve ``enabled`` flag.
- ``_gh_key``: per-type key generation.
- ``_count_sources``: counts enabled sources by type.

All tests construct ``AIConfig`` programmatically and round-trip through
``Config.model_validate`` to confirm structural correctness.
"""

from __future__ import annotations

import pytest

from src.models import (
    AIConfig,
    AIProvider,
    Config,
    FilteringConfig,
    SourcesConfig,
)
from src.setup.wizard import _count_sources, _gh_key, build_config, merge_configs


@pytest.fixture
def ai_config() -> AIConfig:
    return AIConfig(provider=AIProvider.OPENAI, model="deepseek-chat", api_key_env="OPENAI_API_KEY")


# ---------------------------------------------------------------------------
# _gh_key
# ---------------------------------------------------------------------------


def test_gh_key_user_events() -> None:
    from src.models import GitHubSourceConfig

    src = GitHubSourceConfig(type="user_events", username="alice")
    assert _gh_key(src) == "user:alice"


def test_gh_key_repo_releases() -> None:
    from src.models import GitHubSourceConfig

    src = GitHubSourceConfig(type="repo_releases", owner="x", repo="y")
    assert _gh_key(src) == "repo:x/y"


# ---------------------------------------------------------------------------
# build_config
# ---------------------------------------------------------------------------


def test_build_config_empty_selected_yields_hackernews_only(
    ai_config: AIConfig,
) -> None:
    cfg = build_config(ai_config, [])
    assert isinstance(cfg, Config)
    # HackerNews is always-on default.
    assert cfg.sources.hackernews.enabled is True
    assert cfg.sources.github == []
    assert cfg.sources.rss == []


def test_build_config_includes_github_user_source(
    ai_config: AIConfig,
) -> None:
    sels = [
        {
            "type": "github_user",
            "description": "follow alice",
            "config": {"username": "alice"},
        }
    ]
    cfg = build_config(ai_config, sels)
    assert len(cfg.sources.github) == 1
    assert cfg.sources.github[0].username == "alice"
    assert cfg.sources.github[0].type == "user_events"
    assert cfg.sources.github[0].enabled is True


def test_build_config_includes_github_repo_source(
    ai_config: AIConfig,
) -> None:
    sels = [
        {
            "type": "github_repo",
            "description": "track tokio",
            "config": {"owner": "tokio-rs", "repo": "tokio"},
        }
    ]
    cfg = build_config(ai_config, sels)
    assert len(cfg.sources.github) == 1
    assert cfg.sources.github[0].type == "repo_releases"
    assert cfg.sources.github[0].owner == "tokio-rs"
    assert cfg.sources.github[0].repo == "tokio"


def test_build_config_includes_rss_source(
    ai_config: AIConfig,
) -> None:
    sels = [
        {
            "type": "rss",
            "description": "python weekly",
            "config": {"name": "PW", "url": "https://pyweekly.com/rss", "category": "py"},
        }
    ]
    cfg = build_config(ai_config, sels)
    assert len(cfg.sources.rss) == 1
    src = cfg.sources.rss[0]
    assert str(src.url) == "https://pyweekly.com/rss"
    assert src.name == "PW"
    assert src.category == "py"
    assert src.enabled is True


def test_build_config_includes_reddit_subreddit_source(
    ai_config: AIConfig,
) -> None:
    sels = [
        {
            "type": "reddit_subreddit",
            "description": "python subreddit",
            "config": {"subreddit": "python", "sort": "new", "fetch_limit": 10, "min_score": 30},
        }
    ]
    cfg = build_config(ai_config, sels)
    assert cfg.sources.reddit.enabled is True
    assert len(cfg.sources.reddit.subreddits) == 1
    sub = cfg.sources.reddit.subreddits[0]
    assert sub.subreddit == "python"
    assert sub.sort == "new"
    assert sub.fetch_limit == 10
    assert sub.min_score == 30


def test_build_config_includes_reddit_user_source(
    ai_config: AIConfig,
) -> None:
    sels = [
        {
            "type": "reddit_user",
            "description": "u/bob",
            "config": {"username": "bob"},
        }
    ]
    cfg = build_config(ai_config, sels)
    assert cfg.sources.reddit.enabled is True
    assert len(cfg.sources.reddit.users) == 1
    assert cfg.sources.reddit.users[0].username == "bob"


def test_build_config_includes_telegram_channel_source(
    ai_config: AIConfig,
) -> None:
    sels = [
        {
            "type": "telegram",
            "description": "zaihuapd",
            "config": {"channel": "zaihuapd", "fetch_limit": 25},
        }
    ]
    cfg = build_config(ai_config, sels)
    assert cfg.sources.telegram.enabled is True
    assert len(cfg.sources.telegram.channels) == 1
    assert cfg.sources.telegram.channels[0].channel == "zaihuapd"


def test_build_config_reddit_disabled_when_no_subs_or_users(
    ai_config: AIConfig,
) -> None:
    cfg = build_config(ai_config, [])
    assert cfg.sources.reddit.enabled is False


def test_build_config_always_includes_hackernews_default(
    ai_config: AIConfig,
) -> None:
    sels = [{"type": "rss", "config": {"name": "x", "url": "https://x.com/rss"}}]
    cfg = build_config(ai_config, sels)
    # Even when other sources provided, HN should default to enabled.
    assert cfg.sources.hackernews.enabled is True


# ---------------------------------------------------------------------------
# merge_configs
# ---------------------------------------------------------------------------


def _make_base_config() -> Config:
    return Config(
        version="1.0",
        ai=AIConfig(provider=AIProvider.OPENAI, model="m", api_key_env="OPENAI_API_KEY"),
        sources=SourcesConfig(
            github=[
                {"type": "user_events", "username": "alice", "enabled": True},
                {"type": "user_events", "username": "bob", "enabled": False},
            ],
            hackernews=__import__("src.models", fromlist=["HackerNewsConfig"]).HackerNewsConfig(enabled=True),
            rss=[
                {"name": "feedA", "url": "https://a.com/rss", "enabled": True, "category": None},
            ],
            reddit=__import__("src.models", fromlist=["RedditConfig"]).RedditConfig(
                enabled=True,
                subreddits=[__import__("src.models", fromlist=["RedditSubredditConfig"]).RedditSubredditConfig(subreddit="python")],
            ),
            telegram=__import__("src.models", fromlist=["TelegramConfig"]).TelegramConfig(enabled=False),
        ),
        filtering=FilteringConfig(ai_score_threshold=7.0, time_window_hours=24),
    )


def test_merge_configs_dedups_github_by_id(
    ai_config: AIConfig,
) -> None:
    base = _make_base_config()
    new_config = build_config(ai_config, [
        {"type": "github_user", "config": {"username": "alice"}},
        {"type": "github_user", "config": {"username": "carol"}},
    ])
    merged = merge_configs(new_config, base)

    usernames = [s.username for s in merged.sources.github]
    assert usernames.count("alice") == 1
    assert "carol" in usernames
    assert "bob" in usernames  # preserved from existing config.


def test_merge_configs_preserves_existing_enabled_for_dup(
    ai_config: AIConfig,
) -> None:
    """A duplicate source should inherit the existing ``enabled`` value, not the new one."""

    base = _make_base_config()
    # In base, alice is enabled=True. Override via new_config with enabled=False.
    new_config = build_config(ai_config, [
        {"type": "github_user", "config": {"username": "alice"}},
    ])
    # Manually flip the merged.alice.enabled to False to simulate "user deselected".
    new_config.sources.github[0].enabled = False

    merged = merge_configs(new_config, base)

    alice = next(s for s in merged.sources.github if s.username == "alice")
    # Existing enabled state wins.
    assert alice.enabled is True


def test_merge_configs_dedups_rss_by_url(
    ai_config: AIConfig,
) -> None:
    base = _make_base_config()
    new_config = build_config(ai_config, [
        {"type": "rss", "config": {"name": "feedA", "url": "https://a.com/rss"}},
    ])
    merged = merge_configs(new_config, base)

    rss_urls = [str(s.url) for s in merged.sources.rss]
    assert rss_urls.count("https://a.com/rss") == 1


def test_merge_configs_dedups_reddit_subreddits(
    ai_config: AIConfig,
) -> None:
    base = _make_base_config()
    new_config = build_config(ai_config, [
        {"type": "reddit_subreddit", "config": {"subreddit": "python"}},
    ])
    merged = merge_configs(new_config, base)

    subs = [s.subreddit for s in merged.sources.reddit.subreddits]
    assert subs.count("python") == 1


def test_merge_configs_appends_new_subreddit(
    ai_config: AIConfig,
) -> None:
    base = _make_base_config()
    new_config = build_config(ai_config, [
        {"type": "reddit_subreddit", "config": {"subreddit": "rust"}},
    ])
    merged = merge_configs(new_config, base)
    subs = [s.subreddit for s in merged.sources.reddit.subreddits]
    assert "python" in subs
    assert "rust" in subs


def test_merge_configs_ai_is_replaced(ai_config: AIConfig) -> None:
    base = _make_base_config()
    new_config = build_config(ai_config, [])
    merged = merge_configs(new_config, base)
    # AI model should be the new one.
    assert merged.ai.model == "deepseek-chat"


# ---------------------------------------------------------------------------
# _count_sources
# ---------------------------------------------------------------------------


def test_count_sources_zero_when_all_disabled(
    ai_config: AIConfig,
) -> None:
    from src.models import HackerNewsConfig, RedditConfig, SourcesConfig, TelegramConfig

    cfg = Config(
        version="1.0",
        ai=ai_config,
        sources=SourcesConfig(
            hackernews=HackerNewsConfig(enabled=False),
            reddit=RedditConfig(enabled=False),
            telegram=TelegramConfig(enabled=False),
        ),
        filtering=FilteringConfig(ai_score_threshold=7.0, time_window_hours=24),
    )
    assert _count_sources(cfg) == 0


def test_count_sources_hackernews_counts_one(
    ai_config: AIConfig,
) -> None:
    cfg = build_config(ai_config, [])
    # build_config always enables hn.
    assert _count_sources(cfg) == 1


def test_count_sources_reddit_subreddits_and_users(
    ai_config: AIConfig,
) -> None:
    sels = [
        {"type": "reddit_subreddit", "config": {"subreddit": "a"}},
        {"type": "reddit_subreddit", "config": {"subreddit": "b"}},
        {"type": "reddit_user", "config": {"username": "u"}},
    ]
    cfg = build_config(ai_config, sels)
    # 1 HN + 2 subreddits + 1 user = 4
    assert _count_sources(cfg) == 4


def test_count_sources_telegram_channels(
    ai_config: AIConfig,
) -> None:
    sels = [
        {"type": "telegram", "config": {"channel": "x"}},
        {"type": "telegram", "config": {"channel": "y"}},
        {"type": "telegram", "config": {"channel": "z"}},
    ]
    cfg = build_config(ai_config, sels)
    # 1 HN + 3 telegram channels = 4
    assert _count_sources(cfg) == 4


def test_count_sources_github_disabled_excluded(
    ai_config: AIConfig,
) -> None:
    sels = [{"type": "github_user", "config": {"username": "alice"}}]
    cfg = build_config(ai_config, sels)
    # 1 HN + 1 github = 2 (github constructed as enabled=True).
    assert _count_sources(cfg) == 2
    # Now disable github sources → only HN counts.
    for s in cfg.sources.github:
        s.enabled = False
    assert _count_sources(cfg) == 1
