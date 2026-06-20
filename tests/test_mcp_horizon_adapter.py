"""Phase 5 unit tests for ``src.mcp.horizon_adapter`` adapter layer.

Covers ``apply_source_filter`` (clone + selective-disable by source name),
``get_enabled_sources`` (read-only aggregation), ``items_to_dicts`` /
``dicts_to_items`` round-trips, ``get_source_counts`` histogram, regulatory
``_load_mcp_secrets`` (env override, invalid keys skipped, missing handled),
``_is_horizon_repo`` (src/main.py + pyproject.toml gate). ``load_runtime`` /
``resolve_horizon_path`` / ``resolve_config_path`` are exercised only at the
shallow guarantee level; their full paths require a real Horizon repo and are
left to integration smoke tests.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from src.mcp.errors import HorizonMcpError
from src.mcp.horizon_adapter import (
    ENV_KEY_RE,
    VALID_SOURCES,
    _is_horizon_repo,
    _load_mcp_secrets,
    _resolve_secrets_path,
    apply_source_filter,
    dicts_to_items,
    get_enabled_sources,
    get_source_counts,
    items_to_dicts,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_valid_sources_constant() -> None:
    assert {
        "github",
        "hackernews",
        "rss",
        "reddit",
        "telegram",
        "twitter",
        "openbb",
    } == VALID_SOURCES


def test_env_key_re_matches_legitimate_env_var_names() -> None:
    assert ENV_KEY_RE.fullmatch("FOO_BAR")
    assert ENV_KEY_RE.fullmatch("API_KEY")
    assert ENV_KEY_RE.fullmatch("PATH")  # pre-existing env names.
    assert not ENV_KEY_RE.fullmatch("foo")  # lowercase
    assert not ENV_KEY_RE.fullmatch("123FOO")  # starts with digit
    assert not ENV_KEY_RE.fullmatch("FOO-BAR")  # dash


# ---------------------------------------------------------------------------
# _is_horizon_repo
# ---------------------------------------------------------------------------


def test_is_horizon_repo_true_when_main_py_and_pyproject_present(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("# stub")
    (tmp_path / "pyproject.toml").write_text("# stub")
    assert _is_horizon_repo(tmp_path) is True


def test_is_horizon_repo_false_when_missing_main(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("# stub")
    assert _is_horizon_repo(tmp_path) is False


def test_is_horizon_repo_false_when_missing_pyproject(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("# stub")
    assert _is_horizon_repo(tmp_path) is False


# ---------------------------------------------------------------------------
# apply_source_filter
# ---------------------------------------------------------------------------


def test_apply_source_filter_no_sources_returns_original(
    minimal_config: Any,
) -> None:
    cfg, _, _ = apply_source_filter(minimal_config, None)
    assert cfg is minimal_config
    enabled = get_enabled_sources(minimal_config)
    assert enabled == ["hackernews", "rss", "reddit", "telegram"]
    # No '' entries
    assert all(s for s in enabled)


def test_apply_source_filter_known_sources_disables_others(
    minimal_config: Any,
) -> None:
    clone, chosen, unknown = apply_source_filter(minimal_config, ["github", "rss"])
    assert chosen == ["github", "rss"]
    assert unknown == []
    assert clone.sources.hackernews.enabled is False


def test_apply_source_filter_unknown_sources_listed(
    minimal_config: Any,
) -> None:
    clone, chosen, unknown = apply_source_filter(
        minimal_config, ["github", "fakesrc"]
    )
    assert chosen == ["github"]
    assert unknown == ["fakesrc"]


def test_apply_source_filter_disables_reddit_subreddits_and_users(
    minimal_config: Any,
) -> None:
    clone, _, _ = apply_source_filter(minimal_config, ["hackernews"])
    assert clone.sources.reddit.enabled is False
    assert clone.sources.reddit.subreddits == []
    assert clone.sources.reddit.users == []


def test_apply_source_filter_clears_telegram_channels(
    minimal_config: Any,
) -> None:
    clone, _, _ = apply_source_filter(minimal_config, ["hackernews"])
    assert clone.sources.telegram.channels == []
    assert clone.sources.telegram.enabled is False


def test_apply_source_filter_does_not_mutate_input(
    minimal_config: Any,
) -> None:
    original_subreddits = list(minimal_config.sources.reddit.subreddits)
    clone, _, _ = apply_source_filter(minimal_config, ["hackernews"])
    assert minimal_config.sources.reddit.subreddits == original_subreddits
    assert clone is not minimal_config


# ---------------------------------------------------------------------------
# get_enabled_sources
# ---------------------------------------------------------------------------


def test_get_enabled_sources_skips_ossinsight_when_disabled(
    minimal_config: Any,
) -> None:
    enabled = get_enabled_sources(minimal_config)
    # OSSInsight is disabled in our fixture.
    assert "ossinsight" not in enabled


# ---------------------------------------------------------------------------
# items_to_dicts / dicts_to_items / get_source_counts
# ---------------------------------------------------------------------------


def test_items_to_dicts_serializes(
    minimal_runtime: Any,
) -> None:
    items = [minimal_runtime["example_item"]]
    out = items_to_dicts(items)
    assert len(out) == 1
    assert out[0]["id"] == "rss:x:1"
    assert out[0]["title"] == "Title"


def test_dicts_to_items_round_trip(minimal_runtime: Any) -> None:
    items = [minimal_runtime["example_item"]]
    dicts = items_to_dicts(items)
    restored = dicts_to_items(minimal_runtime["runtime"], dicts)
    assert len(restored) == 1
    assert restored[0].id == items[0].id
    assert restored[0].title == items[0].title


def test_get_source_counts(minimal_runtime: Any) -> None:
    ex = minimal_runtime["example_item"]
    src = minimal_runtime["example_item_secondary"]
    counts = get_source_counts([ex, ex, src])
    assert counts["rss"] == 2
    assert counts["github"] == 1


# ---------------------------------------------------------------------------
# _resolve_secrets_path
# ---------------------------------------------------------------------------


def test_resolve_secrets_path_returns_none_when_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HORIZON_MCP_SECRETS_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    out = _resolve_secrets_path(tmp_path)
    assert out is None


def test_resolve_secrets_path_raises_when_explicit_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(tmp_path / "nope.json"))
    with pytest.raises(HorizonMcpError, match="HZ_SECRETS_NOT_FOUND"):
        _resolve_secrets_path(tmp_path)


def test_resolve_secrets_path_returns_explicit_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "secrets.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(target))
    assert _resolve_secrets_path(tmp_path) == target.resolve()


# ---------------------------------------------------------------------------
# _load_mcp_secrets
# ---------------------------------------------------------------------------


def test_load_mcp_secrets_no_file_returns_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HORIZON_MCP_SECRETS_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    # No-op, must not raise.
    _load_mcp_secrets(tmp_path)


def test_load_mcp_secrets_injects_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"env": {"TEST_KEY_ALPHA": "v1", "TEST_KEY_BETA": "v2"}}
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(path))
    monkeypatch.delenv("TEST_KEY_ALPHA", raising=False)
    monkeypatch.delenv("TEST_KEY_BETA", raising=False)

    _load_mcp_secrets(tmp_path)
    assert os.environ.get("TEST_KEY_ALPHA") == "v1"
    assert os.environ.get("TEST_KEY_BETA") == "v2"


def test_load_mcp_secrets_skips_invalid_key_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.json"
    # Lowercase + hyphen keys should be silently skipped by ENV_KEY_RE.
    path.write_text(json.dumps({"env": {"lower-case": "x", "1STARTS_WITH_DIGIT": "y"}}),
                    encoding="utf-8")
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(path))
    _load_mcp_secrets(tmp_path)
    assert "lower-case" not in os.environ
    assert "1STARTS_WITH_DIGIT" not in os.environ


def test_load_mcp_secrets_skips_empty_string_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"env": {"TEST_KEY_EMPTY": "  "}}), encoding="utf-8")
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(path))
    monkeypatch.delenv("TEST_KEY_EMPTY", raising=False)
    _load_mcp_secrets(tmp_path)
    assert "TEST_KEY_EMPTY" not in os.environ


def test_load_mcp_secrets_flat_dict_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If 'env' key absent, the top-level dict is treated as the env payload."""

    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"TEST_KEY_FLAT": "flat_value"}), encoding="utf-8")
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(path))
    monkeypatch.delenv("TEST_KEY_FLAT", raising=False)
    _load_mcp_secrets(tmp_path)
    assert os.environ.get("TEST_KEY_FLAT") == "flat_value"


def test_load_mcp_secrets_invalid_json_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.json"
    path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(path))
    with pytest.raises(HorizonMcpError, match="HZ_SECRETS_INVALID"):
        _load_mcp_secrets(tmp_path)


def test_load_mcp_secrets_top_level_not_dict_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(path))
    with pytest.raises(HorizonMcpError, match="must be a JSON object"):
        _load_mcp_secrets(tmp_path)


def test_load_mcp_secrets_env_field_not_dict_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"env": ["not a dict"]}), encoding="utf-8")
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(path))
    with pytest.raises(HorizonMcpError, match="env field"):
        _load_mcp_secrets(tmp_path)


def test_load_mcp_secrets_non_string_value_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"env": {"TEST_KEY_INT": 42}}), encoding="utf-8")
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(path))
    with pytest.raises(HorizonMcpError, match="must be a string"):
        _load_mcp_secrets(tmp_path)


def test_load_mcp_secrets_override_false_preserves_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"env": {"TEST_KEY_PRESERVE": "from-secrets"}}), encoding="utf-8")
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(path))
    monkeypatch.setenv("TEST_KEY_PRESERVE", "from-env")
    _load_mcp_secrets(tmp_path, override=False)
    assert os.environ["TEST_KEY_PRESERVE"] == "from-env"


def test_load_mcp_secrets_override_true_replaces_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"env": {"TEST_KEY_OVERRIDE": "from-secrets"}}), encoding="utf-8")
    monkeypatch.setenv("HORIZON_MCP_SECRETS_PATH", str(path))
    monkeypatch.setenv("TEST_KEY_OVERRIDE", "from-env")
    _load_mcp_secrets(tmp_path, override=True)
    assert os.environ["TEST_KEY_OVERRIDE"] == "from-secrets"
