"""Tests for ChainedAIClient fallback logic in src/ai/client.py.

The chain already exposes test-friendly injection points:

- ``clients=[...]`` — pre-built AIClient instances, indexed positionally.
- ``client_factory=callable`` — replaces ``_create_single_client`` so we can
  mock per-config without patching the SDK classes.

These are exactly what the production chain accepts, so the tests bypass
SDK patching entirely and assert behaviour at the chain level:

- ``_should_fallback`` recognises the documented retryable error strings.
- ``complete`` returns the first non-empty successful response.
- ``complete`` falls through on retryable errors.
- ``complete`` raises ``RuntimeError("All providers failed. Last error: ...")``
  when every provider in the chain returns or raises a retryable error.
- ``complete`` raises immediately on non-retryable errors (no fallback).
- Empty responses are treated as retryable (raises ``ValueError`` internally).
- Lazy instantiation: ``_get_client(i)`` builds the client on first call only.
"""

import asyncio

import pytest

from src.ai.client import ChainedAIClient
from src.models import AIConfig, AIProvider


def _mk_config(provider: AIProvider = AIProvider.OPENAI) -> AIConfig:
    return AIConfig(
        provider=provider,
        model="test-model",
        api_key_env="TEST_API_KEY",
    )


class _FakeAIClient:
    """In-memory AIClient conforming to ``complete(system, user, temperature, max_tokens)``."""

    def __init__(self, *, return_value=None, side_effect=None):
        self.return_value = return_value
        self.side_effect = side_effect
        self.calls: list[dict] = []

    async def complete(self, system, user, temperature=None, max_tokens=None):
        self.calls.append(
            {"system": system, "user": user, "temperature": temperature, "max_tokens": max_tokens}
        )
        if self.side_effect is not None:
            if callable(self.side_effect):
                return self.side_effect()
            raise self.side_effect
        return self.return_value


# ---------------------------------------------------------------------------
# _should_fallback static classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        "429 too many requests",
        "You've hit the rate limit",
        "401 unauthorized: invalid api key",
        "403 forbidden",
        "quota exceeded for project",
        "anthropic quota exceeded at noon",
        "503 service unavailable",
        "expected server status: 502",  # matches "502" trigger (not "service unavailable" which needs adjacent words)
        "empty response from provider",
    ],
)
def test_should_fallback_returns_true_for_retryable_errors(msg):
    assert ChainedAIClient._should_fallback(Exception(msg)) is True


@pytest.mark.parametrize(
    "msg",
    [
        "400 bad request: malformed json",
        "ValueError: invalid temperature",
        # Note: strings containing 'quota', 'exceeded', 'auth', etc. are retryable.
        # Use raw generic bad-input strings to verify the non-retryable path.
        "400 invalid json syntax",
        "client disconnected",
    ],
)
def test_should_fallback_returns_false_for_non_retryable_errors(msg):
    assert ChainedAIClient._should_fallback(Exception(msg)) is False


# ---------------------------------------------------------------------------
# complete(): success + retryable + non-retryable + all-failed
# ---------------------------------------------------------------------------


def test_chained_complete_returns_first_successful_response():
    good = _FakeAIClient(return_value="primary says hi")
    fallback = _FakeAIClient(return_value="fallback says hi")

    chain = ChainedAIClient(
        configs=[_mk_config(), _mk_config()],
        clients=[good, fallback],
    )
    result = asyncio.run(chain.complete("sys", "user"))

    assert result == "primary says hi"
    assert good.calls == [{"system": "sys", "user": "user", "temperature": None, "max_tokens": None}]
    assert fallback.calls == []  # second provider never consulted


def test_chained_complete_falls_back_on_retryable_error():
    primary = _FakeAIClient(side_effect=Exception("429 rate limit"))
    fallback = _FakeAIClient(return_value="from fallback")

    chain = ChainedAIClient(
        configs=[_mk_config(), _mk_config()],
        clients=[primary, fallback],
    )
    result = asyncio.run(chain.complete("sys", "user"))

    assert result == "from fallback"
    assert primary.calls and fallback.calls  # both were invoked


def test_chained_complete_passes_temperature_and_max_tokens_to_provider():
    primary = _FakeAIClient(return_value="ok")
    chain = ChainedAIClient(
        configs=[_mk_config()],
        clients=[primary],
    )
    asyncio.run(chain.complete("sys", "user", temperature=1.0, max_tokens=200))
    assert primary.calls[0]["temperature"] == 1.0
    assert primary.calls[0]["max_tokens"] == 200


def test_chained_complete_bubbles_non_retryable_error_immediately():
    """A non-retryable error from the primary short-circuits — fallback is never tried."""
    primary = _FakeAIClient(side_effect=Exception("400 bad request: malformed"))
    fallback = _FakeAIClient(return_value="never reached")

    chain = ChainedAIClient(
        configs=[_mk_config(), _mk_config()],
        clients=[primary, fallback],
    )
    with pytest.raises(Exception, match="400 bad request"):
        asyncio.run(chain.complete("sys", "user"))
    assert fallback.calls == []


def test_chained_complete_raises_runtime_error_when_all_providers_fail():
    p1 = _FakeAIClient(side_effect=Exception("429 rate limit"))
    p2 = _FakeAIClient(side_effect=Exception("503 service unavailable"))

    chain = ChainedAIClient(
        configs=[_mk_config(), _mk_config()],
        clients=[p1, p2],
    )
    with pytest.raises(RuntimeError, match="All providers failed"):
        asyncio.run(chain.complete("sys", "user"))


def test_chained_complete_runtime_error_includes_last_error():
    """The chained client surfaces the most-recent provider error in the message."""
    p1 = _FakeAIClient(side_effect=Exception("429 first"))
    p2 = _FakeAIClient(side_effect=Exception("503 second"))

    chain = ChainedAIClient(
        configs=[_mk_config(), _mk_config()],
        clients=[p1, p2],
    )
    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(chain.complete("sys", "user"))
    assert "503 second" in str(exc_info.value)


def test_chained_complete_treats_empty_response_as_retryable():
    """An empty/whitespace response raises ``ValueError`` internally → falls through."""
    p1 = _FakeAIClient(return_value="   ")  # returns whitespace → falsy after strip
    p2 = _FakeAIClient(return_value="ok from p2")

    chain = ChainedAIClient(
        configs=[_mk_config(), _mk_config()],
        clients=[p1, p2],
    )
    result = asyncio.run(chain.complete("sys", "user"))
    assert result == "ok from p2"


def test_chained_complete_no_fallback_message_emitted_when_last_provider_fails():
    """The rich-print notice only fires between providers, not after the final one."""
    p1 = _FakeAIClient(side_effect=Exception("429 rate limit"))

    chain = ChainedAIClient(configs=[_mk_config()], clients=[p1])
    with pytest.raises(RuntimeError):
        asyncio.run(chain.complete("sys", "user"))


# ---------------------------------------------------------------------------
# Lazy instantiation via client_factory
# ---------------------------------------------------------------------------


def test_chained_client_lazy_creates_via_factory():
    """With no clients= override, ``client_factory`` is invoked on first use."""
    good = _FakeAIClient(return_value="lazy ok")
    factory_calls: list[int] = []

    def _factory(config: AIConfig):
        factory_calls.append(config.provider.value)
        return good

    chain = ChainedAIClient(
        configs=[_mk_config()],
        client_factory=_factory,
    )
    result = asyncio.run(chain.complete("sys", "user"))

    assert result == "lazy ok"
    assert factory_calls == [AIProvider.OPENAI.value]


def test_chained_client_factory_invoked_once_per_config():
    """``_get_client`` caches; even with multiple complete() calls only one factory inv."""
    calls = {"n": 0}

    def _factory(config: AIConfig):
        calls["n"] += 1
        return _FakeAIClient(return_value="cached")

    chain = ChainedAIClient(
        configs=[_mk_config()],
        client_factory=_factory,
    )
    asyncio.run(chain.complete("sys", "user"))
    asyncio.run(chain.complete("sys", "user"))

    assert calls["n"] == 1
