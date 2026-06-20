"""Phase 6 unit tests for ``src.setup.prompts``.

Sanity-check that the AI recommendation-prompt string constants exist and
contain the expected placeholders / keyword markers.
"""

from __future__ import annotations

from src.setup.prompts import RECOMMEND_SYSTEM, RECOMMEND_USER


def test_recommend_system_non_empty() -> None:
    assert isinstance(RECOMMEND_SYSTEM, str)
    assert len(RECOMMEND_SYSTEM) > 50


def test_recommend_system_mentions_json_response() -> None:
    """The system prompt instructs JSON-only output."""

    assert "JSON" in RECOMMEND_SYSTEM


def test_recommend_user_non_empty() -> None:
    assert isinstance(RECOMMEND_USER, str)
    assert len(RECOMMEND_USER) > 50


def test_recommend_user_has_formattable_placeholders() -> None:
    # ``str.format`` must succeed with {interests} and {existing_sources}.
    out = RECOMMEND_USER.format(
        interests="LLM inference", existing_sources="  - [rss] example",
    )
    assert "LLM inference" in out
    assert "  - [rss] example" in out


def test_recommend_user_lists_all_supported_source_types() -> None:
    """The schema documents all 5 source types as values for ``type``."""

    assert "rss" in RECOMMEND_USER
    assert "reddit_subreddit" in RECOMMEND_USER
    assert "github_user" in RECOMMEND_USER
    assert "github_repo" in RECOMMEND_USER
    assert "telegram" in RECOMMEND_USER


def test_recommend_user_recommends_3_to_8_additional() -> None:
    assert "3-8" in RECOMMEND_USER or "3 to 8" in RECOMMEND_USER
