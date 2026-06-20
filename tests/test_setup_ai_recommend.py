"""Phase 6 unit tests for ``src.setup.ai_recommend``.

Covers ``get_ai_recommendations`` (async) and ``get_ai_recommendations_sync``
(wrapper). We mock ``create_ai_client`` and ``parse_json_response`` so the
tests do not touch a real network.

Test cases:
- Empty/missing response → ``[]``.
- AI client creation raises → ``[]`` (returns rather than crashes).
- ``parse_json_response`` fails → ``[]``.
- Valid JSON → returned list with each item tagged ``origin='ai'``.
- ``client.complete`` raises → ``[]``.
- ``get_ai_recommendations_sync`` wrapper returns the same value.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.setup.ai_recommend import (
    get_ai_recommendations,
    get_ai_recommendations_sync,
)


@pytest.fixture
def minimal_ai_config() -> Any:
    from src.models import AIConfig

    return AIConfig(provider="openai", model="x", api_key_env="OPENAI_API_KEY")


# ---------------------------------------------------------------------------
# get_ai_recommendations — async
# ---------------------------------------------------------------------------


def test_get_ai_recommendations_returns_empty_when_client_creation_fails(
    minimal_ai_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_cfg: Any) -> Any:
        raise ValueError("api key missing")

    monkeypatch.setattr("src.setup.ai_recommend.create_ai_client", boom)
    out = asyncio.run(
        get_ai_recommendations(minimal_ai_config, "rust lang", [])
    )
    assert out == []


def test_get_ai_recommendations_returns_empty_when_parse_fails(
    minimal_ai_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client = MagicMock()
    fake_client.complete = AsyncMock(return_value="not parseable")

    monkeypatch.setattr(
        "src.setup.ai_recommend.create_ai_client", lambda _cfg: fake_client
    )
    out = asyncio.run(
        get_ai_recommendations(minimal_ai_config, "rust lang", [])
    )
    assert out == []


def test_get_ai_recommendations_returns_empty_when_complete_raises(
    minimal_ai_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client = MagicMock()
    fake_client.complete = AsyncMock(side_effect=RuntimeError("api down"))

    monkeypatch.setattr(
        "src.setup.ai_recommend.create_ai_client", lambda _cfg: fake_client
    )
    out = asyncio.run(
        get_ai_recommendations(minimal_ai_config, "rust lang", [])
    )
    assert out == []


def test_get_ai_recommendations_tags_each_with_origin_ai(
    minimal_ai_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = [
        {"type": "rss", "description": "x"},
        {"type": "github_user", "description": "y"},
    ]
    fake_client = MagicMock()
    fake_client.complete = AsyncMock(return_value='{"sources": ' + str(sources).replace("'", '"') + '}')

    monkeypatch.setattr(
        "src.setup.ai_recommend.create_ai_client", lambda _cfg: fake_client
    )
    out = asyncio.run(
        get_ai_recommendations(minimal_ai_config, "rust lang", [])
    )
    assert len(out) == 2
    assert all(src.get("origin") == "ai" for src in out)


def test_get_ai_recommendations_empty_when_sources_key_missing(
    minimal_ai_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client = MagicMock()
    fake_client.complete = AsyncMock(return_value='{"not_sources": []}')

    monkeypatch.setattr(
        "src.setup.ai_recommend.create_ai_client", lambda _cfg: fake_client
    )
    out = asyncio.run(
        get_ai_recommendations(minimal_ai_config, "rust lang", [])
    )
    assert out == []


def test_get_ai_recommendations_includes_existing_sources_in_prompt(
    minimal_ai_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_kwargs: dict[str, Any] = {}

    async def _capture_complete(**kw: Any) -> str:
        captured_kwargs.update(kw)
        return '{"sources": []}'

    fake_client = MagicMock()
    fake_client.complete = _capture_complete

    monkeypatch.setattr(
        "src.setup.ai_recommend.create_ai_client", lambda _cfg: fake_client
    )

    existing = [{"type": "rss", "description": "first rss"}, {"type": "reddit_subreddit", "description": "a sub"}]
    asyncio.run(
        get_ai_recommendations(minimal_ai_config, "rust lang", existing)
    )

    user_prompt = captured_kwargs["user"]
    assert "rust lang" in user_prompt
    assert "[rss] first rss" in user_prompt


def test_get_ai_recommendations_handles_no_existing_sources(
    minimal_ai_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``existing_sources`` empty → prompt contains ``(none)`` placeholder."""

    captured_kwargs: dict[str, Any] = {}

    async def _capture_complete(**kw: Any) -> str:
        captured_kwargs.update(kw)
        return '{"sources": []}'

    fake_client = MagicMock()
    fake_client.complete = _capture_complete

    monkeypatch.setattr(
        "src.setup.ai_recommend.create_ai_client", lambda _cfg: fake_client
    )

    asyncio.run(get_ai_recommendations(minimal_ai_config, "rust lang", []))

    user_prompt = captured_kwargs["user"]
    assert "(none)" in user_prompt


# ---------------------------------------------------------------------------
# get_ai_recommendations_sync — wrapper
# ---------------------------------------------------------------------------


def test_get_ai_recommendations_sync_returns_empty_on_failure(
    minimal_ai_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "src.setup.ai_recommend.create_ai_client",
        lambda _cfg: (_ for _ in ()).throw(ValueError("no key")),
    )
    out = get_ai_recommendations_sync(minimal_ai_config, "rust lang", [])
    assert out == []


def test_get_ai_recommendations_sync_returns_list(
    minimal_ai_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = [{"type": "rss", "description": "x"}]
    fake_client = MagicMock()
    fake_client.complete = AsyncMock(
        return_value='{"sources": ' + str(sources).replace("'", '"') + '}'
    )

    monkeypatch.setattr(
        "src.setup.ai_recommend.create_ai_client", lambda _cfg: fake_client
    )
    out = get_ai_recommendations_sync(minimal_ai_config, "rust lang", [])
    assert len(out) == 1
    assert out[0]["origin"] == "ai"
