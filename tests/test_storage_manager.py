"""Phase 4 unit tests for ``src.storage.manager.StorageManager``.

Covers ``_expand_env_vars`` recursive expansion (string / dict / list / tuple
/ non-string leaves / missing-var-as-is), ``ConfigError`` raising on bad JSON
and bad pydantic validation, config save-with-backup round-trip, subscribers
CRUD including dedup, and ``save_daily_summary`` file write.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from src.models import AIProvider, Config
from src.storage.manager import ConfigError, StorageManager, _expand_env_vars

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def minimal_config_dict() -> dict[str, Any]:
    """Minimal valid config shape (``AIConfig`` + sources defaults)."""

    return {
        "version": "1.0",
        "ai": {
            "provider": "openai",
            "model": "deepseek-chat",
            "api_key_env": "OPENAI_API_KEY",
            "temperature": 0.3,
            "max_tokens": 4096,
        },
        "sources": {
            "github": [],
            "hackernews": {"enabled": True, "fetch_top_stories": 30, "min_score": 100},
            "rss": [],
            "reddit": {"enabled": False, "subreddits": [], "users": []},
            "telegram": {"enabled": False, "channels": []},
            "ossinsight": {"enabled": False},
        },
        "filtering": {"ai_score_threshold": 7.0, "time_window_hours": 24},
    }


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Return a per-test data directory (``tmp/<random>/``)."""

    data = tmp_path / "data"
    data.mkdir()
    return data


@pytest.fixture
def storage(tmp_data_dir: Path) -> StorageManager:
    return StorageManager(data_dir=str(tmp_data_dir))


# ---------------------------------------------------------------------------
# _expand_env_vars
# ---------------------------------------------------------------------------


def test_expand_env_vars_substitutes_string_leaf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_VAR", "value123")
    assert _expand_env_vars("hello ${MY_VAR}!") == "hello value123!"


def test_expand_env_vars_leaves_missing_var_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEFINITELY_NOT_SET", raising=False)
    assert _expand_env_vars("${DEFINITELY_NOT_SET}") == "${DEFINITELY_NOT_SET}"


def test_expand_env_vars_walks_dict_recursively(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X", "ex")
    value = {"a": "${X}", "b": {"nested": "${X}-1"}, "c": 5}
    assert _expand_env_vars(value) == {"a": "ex", "b": {"nested": "ex-1"}, "c": 5}


def test_expand_env_vars_walks_list_recursively(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X", "ex")
    assert _expand_env_vars(["${X}", "plain", "${X}y"]) == ["ex", "plain", "exy"]


def test_expand_env_vars_walks_tuple_recursively(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X", "ex")
    assert _expand_env_vars(("${X}", "${X}2")) == ("ex", "ex2")


def test_expand_env_vars_returns_non_string_leaves_unchanged() -> None:
    # No env vars set; numbers, bools, None pass through bit-for-bit.
    assert _expand_env_vars(1) == 1
    assert _expand_env_vars(1.5) == 1.5
    assert _expand_env_vars(True) is True
    assert _expand_env_vars(None) is None


def test_expand_env_vars_passthrough_when_no_token() -> None:
    assert _expand_env_vars("no tokens anywhere") == "no tokens anywhere"


# ---------------------------------------------------------------------------
# load_config — happy + sad paths
# ---------------------------------------------------------------------------


def test_load_config_expands_env_then_validates(
    storage: StorageManager,
    minimal_config_dict: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_data_dir: Path,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    minimal_config_dict["ai"]["model"] = "gpt-${OPENAI_API_KEY}"
    (tmp_data_dir / "config.json").write_text(
        json.dumps(minimal_config_dict), encoding="utf-8"
    )
    config = storage.load_config()
    assert config.ai.model == "gpt-sk-test"
    assert config.ai.provider == AIProvider.OPENAI


def test_load_config_missing_file_raises_filenotfound(storage: StorageManager) -> None:
    with pytest.raises(FileNotFoundError):
        storage.load_config()


def test_load_config_invalid_json_raises_config_error(
    storage: StorageManager, tmp_data_dir: Path
) -> None:
    (tmp_data_dir / "config.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid JSON"):
        storage.load_config()


def test_load_config_bad_pydantic_raises_config_error(
    storage: StorageManager, tmp_data_dir: Path, minimal_config_dict: dict[str, Any]
) -> None:
    minimal_config_dict["filtering"]["time_window_hours"] = "not-an-int"
    (tmp_data_dir / "config.json").write_text(
        json.dumps(minimal_config_dict), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="Configuration validation failed"):
        storage.load_config()


# ---------------------------------------------------------------------------
# save_config — backup behaviour + JSON shape
# ---------------------------------------------------------------------------


def test_save_config_creates_backup_when_existing(
    storage: StorageManager, tmp_data_dir: Path, minimal_config_dict: dict[str, Any]
) -> None:
    # Pre-populate so backup branch is exercised.
    (tmp_data_dir / "config.json").write_text(json.dumps({"old": True}), encoding="utf-8")

    cfg = Config.model_validate(minimal_config_dict)
    path = storage.save_config(cfg, backup=True)

    assert path.exists()
    assert (tmp_data_dir / "config.json.bak").exists()
    backup = json.loads((tmp_data_dir / "config.json.bak").read_text(encoding="utf-8"))
    assert backup == {"old": True}

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["ai"]["provider"] == "openai"


def test_save_config_no_backup_when_not_existing(
    storage: StorageManager, tmp_data_dir: Path, minimal_config_dict: dict[str, Any]
) -> None:
    cfg = Config.model_validate(minimal_config_dict)
    storage.save_config(cfg, backup=True)
    assert not (tmp_data_dir / "config.json.bak").exists()


def test_save_config_default_backup_true_when_omitted(
    storage: StorageManager, tmp_data_dir: Path, minimal_config_dict: dict[str, Any]
) -> None:
    (tmp_data_dir / "config.json").write_text(
        json.dumps(minimal_config_dict), encoding="utf-8"
    )
    cfg = Config.model_validate(minimal_config_dict)
    storage.save_config(cfg)
    assert (tmp_data_dir / "config.json.bak").exists()


# ---------------------------------------------------------------------------
# save_daily_summary
# ---------------------------------------------------------------------------


def test_save_daily_summary_writes_markdown_to_summaries_dir(
    storage: StorageManager, tmp_data_dir: Path
) -> None:
    path = storage.save_daily_summary("2026-01-15", "# Title\nbody", language="en")
    assert path.name == "horizon-2026-01-15-en.md"
    assert path.parent == tmp_data_dir / "summaries"
    assert path.read_text(encoding="utf-8") == "# Title\nbody"


def test_save_daily_summary_supports_zh_language(
    storage: StorageManager, tmp_data_dir: Path
) -> None:
    path = storage.save_daily_summary("2026-01-15", "中文", language="zh")
    assert path.name == "horizon-2026-01-15-zh.md"


# ---------------------------------------------------------------------------
# subscribers CRUD
# ---------------------------------------------------------------------------


def test_load_subscribers_returns_empty_when_missing(storage: StorageManager) -> None:
    assert storage.load_subscribers() == []


def test_load_subscribers_returns_empty_on_invalid_json(
    storage: StorageManager, tmp_data_dir: Path
) -> None:
    (tmp_data_dir / "subscribers.json").write_text("{not json", encoding="utf-8")
    assert storage.load_subscribers() == []


def test_add_subscriber_then_routes_via_load(
    storage: StorageManager, tmp_data_dir: Path
) -> None:
    (tmp_data_dir / "subscribers.json").write_text(
        json.dumps(["a@x.com"]), encoding="utf-8"
    )

    storage.add_subscriber("b@x.com")
    assert storage.load_subscribers() == ["a@x.com", "b@x.com"]


def test_add_subscriber_no_op_when_duplicate(
    storage: StorageManager, tmp_data_dir: Path
) -> None:
    (tmp_data_dir / "subscribers.json").write_text(
        json.dumps(["dup@x.com"]), encoding="utf-8"
    )
    storage.add_subscriber("dup@x.com")
    assert storage.load_subscribers() == ["dup@x.com"]


def test_remove_subscriber_when_present(
    storage: StorageManager, tmp_data_dir: Path
) -> None:
    (tmp_data_dir / "subscribers.json").write_text(
        json.dumps(["a@x.com", "b@x.com"]), encoding="utf-8"
    )
    storage.remove_subscriber("a@x.com")
    assert storage.load_subscribers() == ["b@x.com"]


def test_remove_subscriber_no_op_when_missing(
    storage: StorageManager, tmp_data_dir: Path
) -> None:
    (tmp_data_dir / "subscribers.json").write_text(
        json.dumps(["a@x.com"]), encoding="utf-8"
    )
    storage.remove_subscriber("nope@x.com")
    assert storage.load_subscribers() == ["a@x.com"]


# ---------------------------------------------------------------------------
# __init__ side effects
# ---------------------------------------------------------------------------


def test_init_creates_data_and_summaries_dirs(tmp_path: Path) -> None:
    d = tmp_path / "fresh"
    StorageManager(data_dir=str(d))
    assert d.is_dir()
    assert (d / "summaries").is_dir()


def test_expand_env_vars_does_not_touch_actual_os_environ_when_substitute(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expansion leaves os.environ unchanged (read-only on missing keys)."""
    initial_len = len(os.environ)
    _expand_env_vars("${DOES_NOT_EXIST_XYZ}")
    assert len(os.environ) == initial_len
    assert "DOES_NOT_EXIST_XYZ" not in os.environ
