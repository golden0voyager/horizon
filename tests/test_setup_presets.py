"""Phase 6 unit tests for ``src.setup.presets``.

Covers keyword matching ``match_domains`` / ``match_sources``, dedup helper
``collect_sources_from_domains``, ``_source_unique_key`` for every source type
branch, the API transform helper ``_transform_api_response`` (kebab-case ids,
name ↔ config rewrite for RSS, subtype stripping for GitHub), and
``load_presets`` falling back to offline mode → ``FileNotFoundError`` when
both API and local file are unavailable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.setup.presets import (
    PRESETS_ENDPOINT,
    _source_unique_key,
    _tag_matches_input,
    _transform_api_response,
    collect_sources_from_domains,
    match_domains,
    match_sources,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_api_payload() -> dict[str, Any]:
    """A horizon-site API-shaped payload used by ``_transform_api_response`` tests."""

    return {
        "categories": [
            {
                "id": "AI_ML",
                "name": "AI & ML",
                "name_zh": "人工智能",
                "keywords": ["ai", "llm"],
                "sources": [
                    {
                        "type": "reddit_subreddit",
                        "description": "Machine Learning",
                        "description_zh": "机器学习",
                        "tags": ["ml", "ai"],
                        "config": {"subreddit": "MachineLearning"},
                    },
                    {
                        "type": "github_user",
                        "description": "A user",
                        "description_zh": "一位用户",
                        "tags": ["ai"],
                        "config": {"username": "alice", "subtype": "user_events"},
                    },
                    {
                        "type": "rss",
                        "name": "Distill.pub",
                        "description": "ML blog",
                        "description_zh": "机器学习博客",
                        "tags": ["ai"],
                        "config": {"url": "https://distill.pub/rss.xml"},
                    },
                ],
            }
        ]
    }


@pytest.fixture
def sample_presets() -> dict[str, Any]:
    """Internal preset format used by ``match_sources`` / ``match_domains``."""

    return {
        "domains": [
            {
                "id": "ai",
                "name": "AI",
                "keywords": ["ai", "llm"],
                "sources": [
                    {
                        "type": "rss",
                        "description": "Distill.pub ML blog",
                        "description_zh": "机器学习博客",
                        "tags": ["ai", "ml"],
                        "config": {"url": "https://distill.pub/rss.xml"},
                    },
                    {
                        "type": "reddit_subreddit",
                        "description": "Machine Learning",
                        "tags": ["ml"],
                        "config": {"subreddit": "MachineLearning"},
                    },
                ],
            },
            {
                "id": "rust",
                "name": "Rust",
                "keywords": ["rust"],
                "sources": [
                    {
                        "type": "github_repo",
                        "description": "tokio-rs",
                        "tags": ["rust"],
                        "config": {"owner": "tokio-rs", "repo": "tokio"},
                    }
                ],
            }
        ]
    }


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_presets_endpoint_contains_hostname() -> None:
    assert "horizon1123.top" in PRESETS_ENDPOINT
    assert PRESETS_ENDPOINT.endswith("/api/presets")


# ---------------------------------------------------------------------------
# _source_unique_key
# ---------------------------------------------------------------------------


def test_source_unique_key_rss() -> None:
    src = {"type": "rss", "config": {"url": "https://x.com/rss"}}
    assert _source_unique_key(src) == "rss:https://x.com/rss"


def test_source_unique_key_reddit_subreddit() -> None:
    src = {"type": "reddit_subreddit", "config": {"subreddit": "python"}}
    assert _source_unique_key(src) == "reddit:python"


def test_source_unique_key_reddit_user() -> None:
    src = {"type": "reddit_user", "config": {"username": "bob"}}
    assert _source_unique_key(src) == "reddit_user:bob"


def test_source_unique_key_github_user() -> None:
    src = {"type": "github_user", "config": {"username": "alice"}}
    assert _source_unique_key(src) == "github_user:alice"


def test_source_unique_key_github_repo() -> None:
    src = {"type": "github_repo", "config": {"owner": "x", "repo": "y"}}
    assert _source_unique_key(src) == "github_repo:x/y"


def test_source_unique_key_telegram() -> None:
    src = {"type": "telegram", "config": {"channel": "zaihuapd"}}
    assert _source_unique_key(src) == "telegram:zaihuapd"


def test_source_unique_key_unknown_type_falls_back_to_json_dump() -> None:
    src = {"type": "unknown", "config": {"k": "v"}}
    key = _source_unique_key(src)
    assert key.startswith("unknown:")
    assert "v" in key


# ---------------------------------------------------------------------------
# match_domains
# ---------------------------------------------------------------------------


def test_match_domains_returns_zero_when_threshold_unmet(
    sample_presets: dict[str, Any]
) -> None:
    out = match_domains("unrelated interest cookies", sample_presets, threshold=0.5)
    assert out == []


def test_match_domains_returns_ai_domain_for_ai_interest(
    sample_presets: dict[str, Any]
) -> None:
    out = match_domains("ai ml", sample_presets, threshold=0.1)
    domain_ids = [d["id"] for d, s in out]
    assert "ai" in domain_ids
    assert all(s >= 0.1 for _, s in out)


def test_match_domains_sorted_by_score_descending(
    sample_presets: dict[str, Any]
) -> None:
    out = match_domains("rust ai ml", sample_presets, threshold=0.1)
    scores = [s for _, s in out]
    assert scores == sorted(scores, reverse=True)


def test_match_domains_empty_presets() -> None:
    out = match_domains("ai", {"domains": []})
    assert out == []


# ---------------------------------------------------------------------------
# match_sources
# ---------------------------------------------------------------------------


def test_match_sources_returns_no_results_when_empty_input(sample_presets: dict[str, Any]) -> None:
    out = match_sources("", sample_presets)
    # tokens = {""}; desc_score + token_score = 0 (no keywords match).
    assert out == []


def test_match_sources_returns_rss_distill_for_ai_interest(
    sample_presets: dict[str, Any]
) -> None:
    out = match_sources("ai ml", sample_presets)
    sources = [src for src, _ in out]
    assert any(s["type"] == "rss" and "distill" in s["config"]["url"] for s in sources)


def test_match_sources_returns_github_repo_for_rust_interest(
    sample_presets: dict[str, Any]
) -> None:
    out = match_sources("rust tokio", sample_presets)
    sources = [src for src, _ in out]
    assert any(
        s["type"] == "github_repo" and s["config"].get("repo") == "tokio"
        for s in sources
    )


def test_match_sources_each_has_origin_preset(
    sample_presets: dict[str, Any]
) -> None:
    out = match_sources("ai ml", sample_presets)
    for src, _ in out:
        assert src.get("origin") == "preset"


def test_match_sources_uses_tag_aliases_for_match(
    sample_presets: dict[str, Any]
) -> None:
    """A user-supplied alias like 'artificial-intelligence' should also match 'ai' tags."""

    out = match_sources("artificial-intelligence", sample_presets)
    sources = [src for src, _ in out]
    assert any(s["type"] == "rss" for s in sources)


def test_match_sources_dedups_by_unique_key(
    sample_presets: dict[str, Any]
) -> None:
    """If two domains both list the same source, only one entry should come back."""

    # Pre-duplicate by injecting a repeat into the same fixture.
    sample_presets["domains"][0]["sources"].append(dict(sample_presets["domains"][0]["sources"][0]))
    out = match_sources("ai ml", sample_presets)
    rss_urls = [
        s["config"]["url"] for s, _ in out
        if s.get("type") == "rss"
    ]
    assert len(rss_urls) == len(set(rss_urls))


# ---------------------------------------------------------------------------
# _tag_matches_input
# ---------------------------------------------------------------------------


def test_tag_matches_input_main_tag() -> None:
    assert _tag_matches_input("ai", set(), "ai ml") is True
    assert _tag_matches_input("ai", {"ai"}, "") is True
    assert _tag_matches_input("something-else", set(), "") is False


def test_tag_matches_input_alias() -> None:
    # 'artificial-intelligence' is an alias of 'ai'.
    assert _tag_matches_input("ai", set(), "artificial-intelligence") is True


def test_tag_matches_input_unknown_tag_with_no_aliases_returns_false() -> None:
    # 'unknown-tag' isn't in TAG_ALIASES → alias list is empty.
    assert _tag_matches_input("unknown-tag", set(), "anything") is False


# ---------------------------------------------------------------------------
# collect_sources_from_domains
# ---------------------------------------------------------------------------


def test_collect_sources_from_domains_dedups_within_a_session(
    sample_presets: dict[str, Any]
) -> None:
    matched = match_domains("ai ml", sample_presets)
    out = collect_sources_from_domains(matched)
    urls = [s["config"]["url"] for s in out if s["type"] == "rss"]
    assert len(urls) == len(set(urls))
    assert all(s.get("origin") == "preset" for s in out)


def test_collect_sources_from_domains_empty_passthrough() -> None:
    out = collect_sources_from_domains([])
    assert out == []


# ---------------------------------------------------------------------------
# _transform_api_response
# ---------------------------------------------------------------------------


def test_transform_api_response_converts_category_ids_to_kebab(
    sample_api_payload: dict[str, Any]
) -> None:
    out = _transform_api_response(sample_api_payload)
    assert out["domains"][0]["id"] == "ai-ml"


def test_transform_api_response_promotes_rss_name_to_config(
    sample_api_payload: dict[str, Any]
) -> None:
    """For RSS sources the horizon-site API stores ``name`` at the source level;
    horizon's ``build_config`` reads it from inside ``config`` instead."""

    out = _transform_api_response(sample_api_payload)
    sources = out["domains"][0]["sources"]
    rss = next(s for s in sources if s["type"] == "rss")
    assert rss["config"].get("name") == "Distill.pub"


def test_transform_api_response_strips_github_subtype(
    sample_api_payload: dict[str, Any]
) -> None:
    """The horizon-site API uses ``subtype`` for GitHub sources; horizon uses ``type``."""

    out = _transform_api_response(sample_api_payload)
    sources = out["domains"][0]["sources"]
    gh = next(s for s in sources if s["type"] == "github_user")
    assert "subtype" not in gh["config"]


def test_transform_api_response_without_categories_returns_empty() -> None:
    out = _transform_api_response({"not_categories": []})
    assert out == {"domains": []}


def test_transform_api_response_defaults_to_empty_string_for_missing_id() -> None:
    out = _transform_api_response(
        {"categories": [{"sources": [{"type": "rss", "config": {}}]}]}
    )
    assert out["domains"][0]["id"] == ""


def test_transform_api_response_preserves_description_zh_and_tags() -> None:
    payload = {
        "categories": [
            {
                "id": "AI_ML",
                "name": "AI",
                "name_zh": "人工智能",
                "keywords": [],
                "sources": [
                    {
                        "type": "rss",
                        "description": "d",
                        "description_zh": "DZ",
                        "tags": ["a", "b"],
                        "config": {"url": "u"},
                    }
                ],
            }
        ]
    }
    out = _transform_api_response(payload)
    s = out["domains"][0]["sources"][0]
    assert s["description"] == "d"
    assert s["description_zh"] == "DZ"
    assert s["tags"] == ["a", "b"]


# ---------------------------------------------------------------------------
# load_presets (offline fallback)
# ---------------------------------------------------------------------------


def test_load_presets_raises_when_both_api_and_local_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HORIZON_OFFLINE", "true")
    with pytest.raises(FileNotFoundError, match="Presets file not found"):

        from src.setup import presets
        presets.load_presets(
            presets_path=str(tmp_path / "no-such.json"), prefer_api=True,
        )


def test_load_presets_loads_from_local_when_api_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HORIZON_OFFLINE", "true")
    payload = {"domains": [{"id": "ai", "sources": [], "keywords": []}]}
    (tmp_path / "p.json").write_text(json.dumps(payload), encoding="utf-8")

    from src.setup import presets
    out = presets.load_presets(
        presets_path=str(tmp_path / "p.json"), prefer_api=True,
    )
    assert out == payload
