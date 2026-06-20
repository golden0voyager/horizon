"""Tests for the factory entry points in src/ai/client.py.

Three layered functions:

- ``create_ai_client(config)`` — top-level entry. Routes to chain if
  ``config.provider_chain`` is set, otherwise single.
- ``_create_single_client(config)`` — picks Anthropic / Azure / Gemini / one
  of 14 OpenAI-compatible providers based on ``config.provider``.
- ``_create_chained_client(config)`` — parses a comma-separated chain of
  providers, builds per-provider ``AIConfig`` instances from
  ``AI_PROVIDER_DEFAULTS`` (in src/models.py), and constructs a
  ``ChainedAIClient``.

These are exercised end-to-end via the public factory; for the
single-client dispatch we verify the SDK class is referenced (which
suffices to prove the branch was taken — full init is covered in
``test_ai_clients_individual.py``).
"""

import pytest

from src.ai.client import (
    AnthropicClient,
    AzureOpenAIClient,
    ChainedAIClient,
    GeminiClient,
    OpenAIClient,
    _create_chained_client,
    _create_single_client,
    create_ai_client,
)
from src.models import AIConfig, AIProvider


def _mk_config(
    provider: AIProvider | None = None,
    *,
    provider_chain: str | None = None,
    model: str = "test-model",
    api_key_env: str = "TEST_API_KEY",
    base_url: str | None = None,
    azure_endpoint_env: str | None = None,
    api_version: str | None = None,
) -> AIConfig:
    cfg_kwargs: dict = {
        "model": model,
        "api_key_env": api_key_env,
        "base_url": base_url,
    }
    if provider is not None:
        cfg_kwargs["provider"] = provider
    if provider_chain is not None:
        cfg_kwargs["provider_chain"] = provider_chain
    return AIConfig(
        azure_endpoint_env=azure_endpoint_env,
        api_version=api_version,
        **cfg_kwargs,
    )


# ---------------------------------------------------------------------------
# create_ai_client (top-level dispatcher)
# ---------------------------------------------------------------------------


def test_create_ai_client_returns_single_when_provider_chain_unset(monkeypatch):
    """No provider_chain → routes to ``_create_single_client``."""
    monkeypatch.setenv("TEST_API_KEY", "sk-x")
    cfg = _mk_config(provider=AIProvider.ANTHROPIC)
    client = create_ai_client(cfg)
    assert isinstance(client, AnthropicClient)


def test_create_ai_client_returns_chained_when_provider_chain_set():
    cfg = _mk_config(
        provider=AIProvider.OPENAI,
        provider_chain="openai,anthropic",
    )
    client = create_ai_client(cfg)
    assert isinstance(client, ChainedAIClient)
    assert len(client.configs) == 2


# ---------------------------------------------------------------------------
# _create_single_client — dispatch per provider
# ---------------------------------------------------------------------------


def test_create_single_client_dispatches_anthropic(monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "sk-x")
    client = _create_single_client(_mk_config(AIProvider.ANTHROPIC))
    assert isinstance(client, AnthropicClient)


def test_create_single_client_dispatches_azure(monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "sk-x")
    monkeypatch.setenv("AZURE_ENDPOINT", "https://x.example")
    cfg = _mk_config(
        AIProvider.AZURE,
        azure_endpoint_env="AZURE_ENDPOINT",
        api_version="2024-02-01",
    )
    client = _create_single_client(cfg)
    assert isinstance(client, AzureOpenAIClient)


def test_create_single_client_dispatches_gemini(monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "sk-gemini")
    client = _create_single_client(_mk_config(AIProvider.GEMINI))
    assert isinstance(client, GeminiClient)


@pytest.mark.parametrize(
    "provider",
    [
        AIProvider.OPENAI,
        AIProvider.ALI,
        AIProvider.DOUBAO,
        AIProvider.MINIMAX,
        AIProvider.DEEPSEEK,
        AIProvider.OLLAMA,
        AIProvider.MODELSCOPE,
        AIProvider.XIAOMIMIMO,
        AIProvider.MOONSHOTAI,
        AIProvider.OPENROUTER,
        AIProvider.GROQ,
        AIProvider.SILICONFLOW,
        AIProvider.NVIDIA,
        AIProvider.SENSENOVA,
    ],
)
def test_create_single_client_dispatches_openai_compatible_providers(monkeypatch, provider):
    """All 14 OpenAI-compatible providers route to ``OpenAIClient``.

    OLLAMA uses the ``"no_key"`` fallback so we deliberately don't set the
    api_key env for it; for the rest we set the env so ``_resolve_api_key``
    can satisfy the Anthropic / OpenAI SDK init.
    """
    if provider != AIProvider.OLLAMA:
        monkeypatch.setenv("TEST_API_KEY", "sk-x")
    else:
        monkeypatch.delenv("TEST_API_KEY", raising=False)
        cfg = _mk_config(provider, api_key_env="")
    if provider == AIProvider.OLLAMA:
        client = _create_single_client(cfg)
    else:
        client = _create_single_client(_mk_config(provider))
    assert isinstance(client, OpenAIClient), f"{provider} should route to OpenAIClient"


# ---------------------------------------------------------------------------
# _create_chained_client — provider chain parsing
# ---------------------------------------------------------------------------


def test_create_chained_client_parses_provider_chain():
    cfg = _mk_config(
        provider=AIProvider.OPENAI,
        provider_chain="openrouter,deepseek,gemini",
    )
    chain = _create_chained_client(cfg)
    assert isinstance(chain, ChainedAIClient)
    assert [c.provider for c in chain.configs] == [
        AIProvider.OPENROUTER,
        AIProvider.DEEPSEEK,
        AIProvider.GEMINI,
    ]


def test_create_chained_client_uses_ai_provider_defaults_per_chain_member():
    """Each chained config picks model + api_key_env from ``AI_PROVIDER_DEFAULTS``."""
    cfg = _mk_config(
        provider=AIProvider.OPENAI,
        provider_chain="deepseek,gemini",
    )
    chain = _create_chained_client(cfg)
    ds, gm = chain.configs
    assert ds.model == "deepseek-chat"
    assert ds.api_key_env == "DEEPSEEK_API_KEY"
    assert gm.model == "gemini-3-flash-preview"
    assert gm.api_key_env == "GOOGLE_API_KEY"


def test_create_chained_client_inherits_fields_from_base_config():
    """Temperature / max_tokens / languages from the base config propagate to each link."""
    cfg = AIConfig(
        provider=AIProvider.OPENAI,
        provider_chain="openrouter,deepseek",
        model="ignored-default",
        api_key_env="IGNORED_ENV",
        temperature=0.42,
        max_tokens=2048,
        languages=["en", "zh"],
    )
    chain = _create_chained_client(cfg)
    for link in chain.configs:
        assert link.temperature == 0.42
        assert link.max_tokens == 2048
        assert link.languages == ["en", "zh"]


def test_create_chained_client_raises_on_empty_chain(monkeypatch):
    """Whitespace-only or empty provider chain → ValueError."""
    monkeypatch.delenv("HORIZON_AI_PROVIDER", raising=False)
    cfg = _mk_config(provider=AIProvider.OPENAI, provider_chain="")
    with pytest.raises(ValueError, match="provider_chain is empty"):
        _create_chained_client(cfg)


def test_create_chained_client_raises_on_invalid_provider_name():
    """An unknown provider name in the chain → ValueError caught by the map/except."""
    cfg = _mk_config(provider=AIProvider.OPENAI, provider_chain="openai,unknown-provider")
    with pytest.raises(ValueError, match="Unsupported AI provider in chain"):
        _create_chained_client(cfg)


def test_create_chained_client_strips_whitespace_around_providers():
    cfg = _mk_config(
        provider=AIProvider.OPENAI,
        provider_chain=" openrouter , deepseek ",
    )
    chain = _create_chained_client(cfg)
    assert [c.provider for c in chain.configs] == [
        AIProvider.OPENROUTER,
        AIProvider.DEEPSEEK,
    ]
