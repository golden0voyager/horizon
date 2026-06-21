"""Layer-1 unit tests for ``src.mcp.horizon_adapter`` adapter layer.

Coverage targets the pure/Python branches that don't need a live
Horizon runtime:

* ``resolve_horizon_path`` honors explicit > env > repo_root > cwd
  fallback chain and raises ``HZ_HORIZON_NOT_FOUND`` if no candidate
  matches.
* ``apply_source_filter`` clones the config and disables every
  source type the caller did not request.
* ``get_enabled_sources`` walks the seven known source types.
* ``get_source_counts`` aggregates by ``item.source_type.value``.
* ``items_to_dicts`` and ``dicts_to_items`` round-trip when the
  runtime exposes ``ContentItem.model_dump`` / ``model_validate``.
* ``_load_mcp_secrets`` rejects non-string values, JSON errors, and
  honors ``override=True``.
* ``_resolve_secrets_path`` honours ``HORIZON_MCP_SECRETS_PATH`` when
  the explicit override leads to an existing file.

The fixtures below use the ``hz_horizon_adapter_`` prefix so they
cannot collide with fixtures in another test module (per the
mcp-001 brief on cross-suite collection shadowing).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.mcp import horizon_adapter
from src.mcp.errors import HorizonMcpError
from src.models import (
    AIProvider,
    Config,
    ContentItem,
    OSSInsightConfig,
    SourceType,
)

# ---------------------------------------------------------------------------
# Configured fixtures (namespaced to avoid shadowing collisions)
# ---------------------------------------------------------------------------


@pytest.fixture(name="hz_horizon_adapter_minimal_repo")
def hz_horizon_adapter_minimal_repo_fixture(tmp_path: Path) -> Path:
    """Build a minimal Horizon repo stub under ``tmp_path``."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    return tmp_path


@pytest.fixture(name="hz_horizon_adapter_minimal_config_dict")
def hz_horizon_adapter_minimal_config_dict_fixture() -> dict:
    return {
        "ai": {
            "provider": "openai",
            "model": "test-model",
            "api_key_env": "OPENAI_API_KEY",
        },
        "sources": {},
        "filtering": {},
    }


# ---------------------------------------------------------------------------
# resolve_horizon_path
# ---------------------------------------------------------------------------


def test_hz_horizon_adapter_resolve_accepts_explicit_repo(
    hz_horizon_adapter_minimal_repo: Path,
) -> None:
    result = horizon_adapter.resolve_horizon_path(str(hz_horizon_adapter_minimal_repo))
    assert result == hz_horizon_adapter_minimal_repo.resolve()


def test_hz_horizon_adapter_resolve_falls_back_to_env(
    hz_horizon_adapter_minimal_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HORIZON_PATH", str(hz_horizon_adapter_minimal_repo))
    result = horizon_adapter.resolve_horizon_path(explicit=None)
    assert result == hz_horizon_adapter_minimal_repo.resolve()


def test_hz_horizon_adapter_resolve_raises_when_no_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Stub ``_is_horizon_repo`` so every candidate path fails its check,
    # forcing ``resolve_horizon_path`` to exhaust the candidates loop and
    # raise ``HZ_HORIZON_NOT_FOUND`` instead of returning the real repo
    # under the test process's CWD.
    monkeypatch.setattr(horizon_adapter, "_is_horizon_repo", lambda path: False)
    explicit = tmp_path / "fake_repo"
    explicit.mkdir()

    with pytest.raises(HorizonMcpError) as exc_info:
        horizon_adapter.resolve_horizon_path(explicit=str(explicit))
    assert exc_info.value.code == "HZ_HORIZON_NOT_FOUND"
    assert "checked" in exc_info.value.details


# ---------------------------------------------------------------------------
# resolve_config_path
# ---------------------------------------------------------------------------


def test_hz_horizon_adapter_resolve_config_path_absolute_existing(
    hz_horizon_adapter_minimal_repo: Path,
) -> None:
    cfg = hz_horizon_adapter_minimal_repo / "data" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{}", encoding="utf-8")
    assert horizon_adapter.resolve_config_path(hz_horizon_adapter_minimal_repo, str(cfg)) == cfg.resolve()


def test_hz_horizon_adapter_resolve_config_path_defaults_to_repo_data(
    hz_horizon_adapter_minimal_repo: Path,
) -> None:
    cfg = hz_horizon_adapter_minimal_repo / "data" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{}", encoding="utf-8")
    assert horizon_adapter.resolve_config_path(hz_horizon_adapter_minimal_repo) == cfg.resolve()


def test_hz_horizon_adapter_resolve_config_path_missing_maps_to_error(
    hz_horizon_adapter_minimal_repo: Path,
) -> None:
    with pytest.raises(HorizonMcpError) as exc_info:
        horizon_adapter.resolve_config_path(hz_horizon_adapter_minimal_repo, "/no/such/path/config.json")
    assert exc_info.value.code == "HZ_CONFIG_NOT_FOUND"


# ---------------------------------------------------------------------------
# apply_source_filter / get_enabled_sources — exercise every branch
# ---------------------------------------------------------------------------


def _full_config() -> Config:
    return Config.model_validate(
        {
            "ai": {
                "provider": AIProvider.OPENAI,
                "model": "test-model",
                "api_key_env": "OPENAI_API_KEY",
            },
            "sources": {
                "github": [{"type": "user_events", "username": "openai"}],
                "hackernews": {"enabled": True},
                "rss": [{"name": "feed-1", "url": "https://example.com/feed.xml"}],
                "reddit": {
                    "enabled": True,
                    "subreddits": [{"subreddit": "LocalLLaMA", "enabled": True}],
                },
                "telegram": {
                    "enabled": True,
                    "channels": [{"channel": "zaihuapd"}],
                },
                "twitter": {"enabled": True, "users": ["openai"]},
                "openbb": {
                    "enabled": True,
                    "watchlists": [{"name": "ai", "symbols": ["NVDA"]}],
                },
                "ossinsight": {"enabled": True},
            },
            "filtering": {},
        }
    )


def test_hz_horizon_adapter_apply_source_filter_disables_unrequested() -> None:
    cfg = _full_config()
    filtered, chosen, unknown = horizon_adapter.apply_source_filter(cfg, ["reddit", "rss"])
    assert chosen == ["reddit", "rss"]
    assert unknown == []
    assert filtered.sources.reddit.enabled is True
    assert filtered.sources.rss == cfg.sources.rss
    assert filtered.sources.github == []
    assert filtered.sources.hackernews.enabled is False
    assert filtered.sources.telegram.enabled is False
    assert filtered.sources.telegram.channels == []
    assert filtered.sources.twitter is not None
    assert filtered.sources.twitter.enabled is False
    assert filtered.sources.twitter.users == []
    assert filtered.sources.openbb is not None
    assert filtered.sources.openbb.enabled is False
    assert filtered.sources.openbb.watchlists == []


def test_hz_horizon_adapter_apply_source_filter_with_no_sources_returns_copy() -> None:
    cfg = _full_config()
    filtered, enabled, unknown = horizon_adapter.apply_source_filter(cfg, None)
    assert unknown == []
    assert filtered.sources.github == cfg.sources.github
    assert enabled == horizon_adapter.get_enabled_sources(filtered)


def test_hz_horizon_adapter_apply_source_filter_reports_unknown_names() -> None:
    cfg = _full_config()
    _, chosen, unknown = horizon_adapter.apply_source_filter(cfg, ["Mastodon", "reddit"])
    assert chosen == ["reddit"]
    assert unknown == ["mastodon"]


def test_hz_horizon_adapter_get_enabled_sources_covers_all_branches() -> None:
    cfg = _full_config()
    enabled = horizon_adapter.get_enabled_sources(cfg)
    for src in (
        "github",
        "hackernews",
        "rss",
        "reddit",
        "telegram",
        "twitter",
        "openbb",
    ):
        assert src in enabled


def test_hz_horizon_adapter_get_enabled_sources_ossinsight_only_adds_no_extra() -> None:
    cfg = Config.model_validate(
        {
            "ai": {
                "provider": AIProvider.OPENAI,
                "model": "test-model",
                "api_key_env": "OPENAI_API_KEY",
            },
            "sources": {
                "ossinsight": OSSInsightConfig(enabled=True).model_dump(),
            },
            "filtering": {},
        }
    )
    # OSSInsight is observation-only; it is not part of the enabled-source
    # gating list returned to the MCP tool surface.
    assert "ossinsight" not in horizon_adapter.get_enabled_sources(cfg)


# ---------------------------------------------------------------------------
# get_source_counts / items_to_dicts / dicts_to_items
# ---------------------------------------------------------------------------


def test_hz_horizon_adapter_get_source_counts_aggregates_by_value() -> None:
    items = [
        SimpleNamespace(source_type=SimpleNamespace(value="github")),
        SimpleNamespace(source_type=SimpleNamespace(value="github")),
        SimpleNamespace(source_type=SimpleNamespace(value="reddit")),
        SimpleNamespace(source_type=SimpleNamespace(value="rss")),
    ]
    counts = horizon_adapter.get_source_counts(items)
    assert counts == {"github": 2, "reddit": 1, "rss": 1}


def test_hz_horizon_adapter_items_to_dicts_zero_input() -> None:
    assert horizon_adapter.items_to_dicts([]) == []


def test_hz_horizon_adapter_dicts_to_items_round_trip() -> None:
    payload = {
        "id": "github:user_events:abc",
        "source_type": SourceType.GITHUB,
        "title": "Round trip",
        "url": "https://example.com/post",
        "content": "body",
        "author": "tester",
        "published_at": datetime.now(UTC).isoformat(),
    }

    class _Runtime:
        ContentItem = ContentItem

    items = horizon_adapter.dicts_to_items(_Runtime(), [payload])
    assert len(items) == 1
    assert items[0].id == payload["id"]
    assert horizon_adapter.items_to_dicts(items)[0]["id"] == payload["id"]


# ---------------------------------------------------------------------------
# _load_mcp_secrets / _resolve_secrets_path
# ---------------------------------------------------------------------------


def test_hz_horizon_adapter_load_secrets_loads_generic_env_keys(
    hz_horizon_adapter_minimal_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loads valid env keys from secrets JSON, ignores non-uppercase keys."""
    secrets_path = hz_horizon_adapter_minimal_repo / "mcp.secrets.json"
    secrets_path.write_text(
        json.dumps({
            "env": {
                "ANTHROPIC_API_KEY": "sk-ant-test",
                "CUSTOM_TOKEN": "token-123",
                "lowercase": "ignored",
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(secrets_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_TOKEN", raising=False)

    horizon_adapter._load_mcp_secrets(hz_horizon_adapter_minimal_repo, override=False)

    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert os.environ["CUSTOM_TOKEN"] == "token-123"
    assert "lowercase" not in os.environ


def test_hz_horizon_adapter_load_secrets_rejects_non_string_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = tmp_path / "secrets.json"
    secrets.write_text(json.dumps({"env": {"GOOD": "ok", "BAD": 12345}}))
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(secrets))
    monkeypatch.delenv("BAD", raising=False)

    with pytest.raises(HorizonMcpError) as exc_info:
        horizon_adapter._load_mcp_secrets(tmp_path, override=True)
    assert exc_info.value.code == "HZ_SECRETS_INVALID"
    assert exc_info.value.details["key"] == "BAD"


def test_hz_horizon_adapter_load_secrets_rejects_invalid_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = tmp_path / "secrets.json"
    secrets.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(secrets))

    with pytest.raises(HorizonMcpError) as exc_info:
        horizon_adapter._load_mcp_secrets(tmp_path, override=True)
    assert exc_info.value.code == "HZ_SECRETS_INVALID"


def test_hz_horizon_adapter_load_secrets_skips_empty_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = tmp_path / "secrets.json"
    secrets.write_text(json.dumps({"env": {"MAYBE": "   "}}))
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(secrets))
    monkeypatch.delenv("MAYBE", raising=False)

    horizon_adapter._load_mcp_secrets(tmp_path, override=True)
    assert "MAYBE" not in __import__("os").environ


def test_hz_horizon_adapter_load_secrets_override_respects_existing_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = tmp_path / "secrets.json"
    secrets.write_text(json.dumps({"env": {"CUSTOM": "from-secrets"}}))
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(secrets))
    monkeypatch.setenv("CUSTOM", "from-env")

    horizon_adapter._load_mcp_secrets(tmp_path, override=False)
    assert __import__("os").environ["CUSTOM"] == "from-env"

    horizon_adapter._load_mcp_secrets(tmp_path, override=True)
    assert __import__("os").environ["CUSTOM"] == "from-secrets"


def test_hz_horizon_adapter_resolve_secrets_path_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = tmp_path / "explicit.json"
    secrets.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(secrets))
    assert horizon_adapter._resolve_secrets_path(tmp_path) == secrets.resolve()


def test_hz_horizon_adapter_resolve_secrets_path_explicit_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing.json"
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(missing))

    with pytest.raises(HorizonMcpError) as exc_info:
        horizon_adapter._resolve_secrets_path(tmp_path)
    assert exc_info.value.code == "HZ_SECRETS_NOT_FOUND"


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def test_hz_horizon_adapter_load_config_expands_env_vars(
    hz_horizon_adapter_minimal_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_config expands ${VAR} placeholders from environment."""
    config_path = hz_horizon_adapter_minimal_repo / "config.json"
    config_path.write_text(
        json.dumps({
            "ai": {
                "provider": "openai",
                "model": "test-model",
                "api_key_env": "OPENAI_API_KEY",
                "base_url": "${TEST_BASE_URL}/v1",
            },
            "sources": {},
            "filtering": {},
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_BASE_URL", "https://api.example.com")

    runtime = horizon_adapter.load_runtime(hz_horizon_adapter_minimal_repo)
    config = horizon_adapter.load_config(runtime, config_path)

    assert config.ai.base_url == "https://api.example.com/v1"


# ---------------------------------------------------------------------------
# misc helpers
# ---------------------------------------------------------------------------


def test_hz_horizon_adapter_is_repo_detects_required_layout(
    tmp_path: Path,
) -> None:
    assert horizon_adapter._is_horizon_repo(tmp_path) is False
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("", encoding="utf-8")
    assert horizon_adapter._is_horizon_repo(tmp_path) is False
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    assert horizon_adapter._is_horizon_repo(tmp_path) is True
