"""Tests for the four SDK-specific client classes in src/ai/client.py.

Each SDK is patched via ``monkeypatch.setattr("src.ai.client.<Class>", FakeCls)``
which replaces the imported reference inside src/ai/client — far simpler than
intercepting httpx transports.

The FakeCls shape mirrors the real SDK's only attributes our code touches:

- ``AsyncAnthropic``: ``.messages.create(**kwargs)`` → returns message with
  ``.content[0].text`` and optional ``.usage.{input_tokens,output_tokens}``.
- ``AsyncOpenAI`` / ``AsyncAzureOpenAI``: ``.chat.completions.create(**kwargs)``
  → returns response with ``.choices[0].message.content`` and optional
  ``.usage.{prompt_tokens,completion_tokens}``.
- ``genai.Client``: ``.aio.models.generate_content(...)`` → returns response
  with ``.text`` and optional ``.usage_metadata.{total_token_count,prompt_token_count}``.

Side effects (``record_usage``) are no-op'd via a list-collector spy to keep
tests hermetic without mocking the call.
"""

import asyncio
from types import SimpleNamespace

from src.ai.client import (
    AnthropicClient,
    AzureOpenAIClient,
    GeminiClient,
    OpenAIClient,
)
from src.models import AIConfig, AIProvider


def _patch_record_usage(monkeypatch):
    """Patch ``src.ai.client.record_usage`` to a list-collecting spy."""
    calls: list[dict] = []

    def _spy(provider, **kw):
        calls.append({"provider": provider, **kw})

    monkeypatch.setattr("src.ai.client.record_usage", _spy)
    return calls


def _mk_config(
    provider: AIProvider,
    *,
    api_key_env: str = "TEST_API_KEY",
    base_url: str | None = None,
    base_url_env: str | None = None,
    model: str = "test-model",
    azure_endpoint_env: str | None = None,
    api_version: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.3,
) -> AIConfig:
    return AIConfig(
        provider=provider,
        model=model,
        api_key_env=api_key_env,
        base_url=base_url,
        base_url_env=base_url_env,
        max_tokens=max_tokens,
        temperature=temperature,
        azure_endpoint_env=azure_endpoint_env,
        api_version=api_version,
    )


# ---------------------------------------------------------------------------
# AnthropicClient
# ---------------------------------------------------------------------------


def test_anthropic_init_passes_base_url_through(monkeypatch):
    captured: dict = {}

    class _Fake:
        def __init__(self, **kwargs):
            captured.clear()
            captured.update(kwargs)

    monkeypatch.setattr("src.ai.client.AsyncAnthropic", _Fake)
    monkeypatch.setenv("TEST_API_KEY", "sk-x")

    AnthropicClient(_mk_config(AIProvider.ANTHROPIC, base_url="https://proxy.example"))
    assert captured["api_key"] == "sk-x"
    assert captured["base_url"] == "https://proxy.example"


def test_anthropic_init_omits_base_url_when_not_configured(monkeypatch):
    captured: dict = {}

    class _Fake:
        def __init__(self, **kwargs):
            captured.clear()
            captured.update(kwargs)

    monkeypatch.setattr("src.ai.client.AsyncAnthropic", _Fake)
    monkeypatch.setenv("TEST_API_KEY", "sk-x")

    AnthropicClient(_mk_config(AIProvider.ANTHROPIC))
    assert "base_url" not in captured


def test_anthropic_complete_returns_text_and_records_usage(monkeypatch):
    record = _patch_record_usage(monkeypatch)
    captured: dict = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(text="hi world")],
                usage=SimpleNamespace(input_tokens=11, output_tokens=22),
            )

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            self.messages = _FakeMessages()

    monkeypatch.setattr("src.ai.client.AsyncAnthropic", _FakeAnthropic)
    monkeypatch.setenv("TEST_API_KEY", "sk-x")

    cfg = _mk_config(AIProvider.ANTHROPIC, model="claude-test")
    result = asyncio.run(AnthropicClient(cfg).complete("sys", "user"))

    assert result == "hi world"
    assert captured["model"] == "claude-test"
    assert captured["system"] == "sys"
    assert captured["messages"] == [{"role": "user", "content": "user"}]
    assert record == [{"provider": "anthropic", "input_tokens": 11, "output_tokens": 22}]


def test_anthropic_complete_handles_missing_usage(monkeypatch):
    record = _patch_record_usage(monkeypatch)

    class _FakeMessages:
        async def create(self, **kwargs):
            return SimpleNamespace(content=[SimpleNamespace(text="answer")], usage=None)

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            self.messages = _FakeMessages()

    monkeypatch.setattr("src.ai.client.AsyncAnthropic", _FakeAnthropic)
    monkeypatch.setenv("TEST_API_KEY", "sk-x")

    cfg = _mk_config(AIProvider.ANTHROPIC)
    result = asyncio.run(AnthropicClient(cfg).complete("sys", "usr"))
    assert result == "answer"
    assert record == []  # no usage → no recording


def test_anthropic_complete_overrides_temperature_and_max_tokens(monkeypatch):
    captured: dict = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(text="x")], usage=None)

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            self.messages = _FakeMessages()

    monkeypatch.setattr("src.ai.client.AsyncAnthropic", _FakeAnthropic)
    monkeypatch.setenv("TEST_API_KEY", "sk-x")

    cfg = _mk_config(AIProvider.ANTHROPIC, temperature=0.3, max_tokens=4096)
    asyncio.run(
        AnthropicClient(cfg).complete("sys", "usr", temperature=0.9, max_tokens=128)
    )
    assert captured["temperature"] == 0.9
    assert captured["max_tokens"] == 128


# ---------------------------------------------------------------------------
# OpenAIClient — incl. matrix quirks for 14 OpenAI-compatible providers
# ---------------------------------------------------------------------------


def _openai_fake(monkeypatch, *, create_result=None, create_side_effect=None):
    """Install a fake ``AsyncOpenAI`` that records ``chat.completions.create`` calls."""
    captured: list[dict] = []

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            if create_side_effect is not None:
                if callable(create_side_effect):
                    result = create_side_effect(**kwargs)
                    # ``create_side_effect`` may be sync or async; await coroutines.
                    if asyncio.iscoroutine(result):
                        return await result
                    return result
                raise create_side_effect
            return create_result

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = SimpleNamespace(completions=_FakeCompletions())

    monkeypatch.setattr("src.ai.client.AsyncOpenAI", _FakeOpenAI)
    return captured


def test_openai_init_ollama_uses_no_key_fallback(monkeypatch):
    """Ollama provides ``api_key="no_key"`` rather than raising on missing env."""
    monkeypatch.delenv("TEST_API_KEY", raising=False)
    captured: list[dict] = []

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr("src.ai.client.AsyncOpenAI", _FakeOpenAI)

    cfg = _mk_config(AIProvider.OLLAMA, api_key_env="")
    OpenAIClient(cfg)
    # No env set, OLLAMA fallback path exercises "no_key"
    assert any(c["api_key"] == "no_key" for c in captured)


def test_openai_init_passes_configured_base_url(monkeypatch):
    captured: list[dict] = []

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr("src.ai.client.AsyncOpenAI", _FakeOpenAI)
    monkeypatch.setenv("TEST_API_KEY", "sk-x")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    cfg = _mk_config(
        AIProvider.OPENAI,
        base_url="https://config.example/v1",
        base_url_env=None,
    )
    OpenAIClient(cfg)
    assert captured[0]["base_url"] == "https://config.example/v1"
    assert captured[0]["api_key"] == "sk-x"


def test_openai_init_uses_provider_default_for_known_providers(monkeypatch):
    """When no base_url configured, OPENAI_BASE_URL clause falls through to per-provider default."""
    captured: list[dict] = []

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr("src.ai.client.AsyncOpenAI", _FakeOpenAI)
    monkeypatch.setenv("TEST_API_KEY", "sk-x")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    cfg = _mk_config(AIProvider.DEEPSEEK)
    OpenAIClient(cfg)
    assert captured[0]["base_url"] == "https://api.deepseek.com"


def test_openai_complete_clamps_temperature_for_minimax(monkeypatch):
    """minimax is in ``_TEMP_CLAMP`` — temperature <=0 becomes 0.01."""
    captured = _openai_fake(
        monkeypatch,
        create_result=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=None,
        ),
    )
    monkeypatch.setenv("TEST_API_KEY", "sk-x")
    cfg = _mk_config(AIProvider.MINIMAX, temperature=0.0)
    asyncio.run(OpenAIClient(cfg).complete("sys", "user"))
    assert captured[0]["temperature"] == 0.01


def test_openai_complete_passes_through_temperature_for_non_clamped_providers(monkeypatch):
    captured = _openai_fake(
        monkeypatch,
        create_result=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=None,
        ),
    )
    monkeypatch.setenv("TEST_API_KEY", "sk-x")
    cfg = _mk_config(AIProvider.OPENAI, temperature=0.0)
    asyncio.run(OpenAIClient(cfg).complete("sys", "user"))
    assert captured[0]["temperature"] == 0.0  # not clamped → preserved


def test_openai_complete_omits_response_format_for_minimax(monkeypatch):
    """minimax is in ``_NO_RESPONSE_FORMAT`` — request_payload must not include it."""
    captured = _openai_fake(
        monkeypatch,
        create_result=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=None,
        ),
    )
    monkeypatch.setenv("TEST_API_KEY", "sk-x")
    cfg = _mk_config(AIProvider.MINIMAX)
    asyncio.run(OpenAIClient(cfg).complete("sys", "user"))
    assert "response_format" not in captured[0]


def test_openai_complete_includes_response_format_for_other_providers(monkeypatch):
    captured = _openai_fake(
        monkeypatch,
        create_result=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=None,
        ),
    )
    monkeypatch.setenv("TEST_API_KEY", "sk-x")
    cfg = _mk_config(AIProvider.OPENAI)
    asyncio.run(OpenAIClient(cfg).complete("sys", "user"))
    assert captured[0]["response_format"] == {"type": "json_object"}


def test_openai_complete_temperature_unsupported_triggers_silent_retry(monkeypatch):
    """First call raises ``Temperature not supported by this model`` → second call drops param."""
    record = _patch_record_usage(monkeypatch)
    calls = {"n": 0}

    async def _create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("temperature is not supported by this model")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="after-retry"))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
        )

    captured = _openai_fake(monkeypatch, create_side_effect=_create)
    monkeypatch.setenv("TEST_API_KEY", "sk-x")
    cfg = _mk_config(AIProvider.OPENAI)

    result = asyncio.run(OpenAIClient(cfg).complete("sys", "user"))
    assert calls["n"] == 2
    assert "temperature" in captured[0]
    assert "temperature" not in captured[1]
    assert result == "after-retry"
    assert record == [{"provider": "openai", "input_tokens": 3, "output_tokens": 4}]


def test_openai_complete_non_temperature_error_propagates(monkeypatch):
    """Errors not matching ``_is_temperature_unsupported`` are re-raised."""
    import pytest

    calls = {"n": 0}

    async def _create(**kwargs):
        calls["n"] += 1
        raise Exception("429 rate limit reached")

    _openai_fake(monkeypatch, create_side_effect=_create)
    monkeypatch.setenv("TEST_API_KEY", "sk-x")
    cfg = _mk_config(AIProvider.OPENAI)

    with pytest.raises(Exception, match="429"):
        asyncio.run(OpenAIClient(cfg).complete("sys", "user"))
    assert calls["n"] == 1  # no retry


# ---------------------------------------------------------------------------
# AzureOpenAIClient
# ---------------------------------------------------------------------------


def _azure_fake(monkeypatch, *, create_side_effect=None, captured=None):
    if captured is None:
        captured = []

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            if create_side_effect is not None:
                if callable(create_side_effect):
                    result = create_side_effect(**kwargs)
                    # ``create_side_effect`` may be sync or async; await coroutines.
                    if asyncio.iscoroutine(result):
                        return await result
                    return result
                raise create_side_effect
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                usage=None,
            )

    class _FakeAzure:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=_FakeCompletions())

    monkeypatch.setattr("src.ai.client.AsyncAzureOpenAI", _FakeAzure)
    return captured


def test_azure_init_requires_azure_endpoint_env(monkeypatch):
    import pytest

    monkeypatch.setenv("TEST_API_KEY", "sk-x")
    cfg = _mk_config(
        AIProvider.AZURE,
        azure_endpoint_env=None,
        api_version="2024-02-01",
    )
    with pytest.raises(ValueError, match="azure_endpoint_env"):
        AzureOpenAIClient(cfg)


def test_azure_init_requires_api_version(monkeypatch):
    import pytest

    monkeypatch.setenv("TEST_API_KEY", "sk-x")
    monkeypatch.setenv("AZURE_ENDPOINT", "https://x.example")
    cfg = _mk_config(
        AIProvider.AZURE,
        azure_endpoint_env="AZURE_ENDPOINT",
        api_version=None,
    )
    with pytest.raises(ValueError, match="api_version"):
        AzureOpenAIClient(cfg)


def test_azure_complete_uses_max_completion_tokens_for_o_series_models(monkeypatch):
    """When model starts with o1/o3/o4/gpt-5, ``max_completion_tokens`` is the kwarg."""
    captured = _azure_fake(monkeypatch)
    monkeypatch.setenv("TEST_API_KEY", "sk-x")
    monkeypatch.setenv("AZURE_ENDPOINT", "https://x.example")

    cfg = _mk_config(
        AIProvider.AZURE,
        model="o1-preview",
        azure_endpoint_env="AZURE_ENDPOINT",
        api_version="2024-02-01",
        max_tokens=256,
    )
    asyncio.run(AzureOpenAIClient(cfg).complete("sys", "user"))
    assert captured[0].get("max_completion_tokens") == 256
    assert "max_tokens" not in captured[0]


def test_azure_complete_uses_max_tokens_for_legacy_models(monkeypatch):
    captured = _azure_fake(monkeypatch)
    monkeypatch.setenv("TEST_API_KEY", "sk-x")
    monkeypatch.setenv("AZURE_ENDPOINT", "https://x.example")

    cfg = _mk_config(
        AIProvider.AZURE,
        model="gpt-4-turbo",
        azure_endpoint_env="AZURE_ENDPOINT",
        api_version="2024-02-01",
        max_tokens=256,
    )
    asyncio.run(AzureOpenAIClient(cfg).complete("sys", "user"))
    assert captured[0]["max_tokens"] == 256
    assert "max_completion_tokens" not in captured[0]


def test_azure_complete_token_kwarg_fallback_from_max_tokens_to_max_completion(monkeypatch):
    """``max_tokens vs max_completion_tokens`` mention → switch to completion-kwarg."""
    captured = _azure_fake(monkeypatch)
    calls = {"n": 0}

    async def _create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("unsupported parameter: max_tokens; use max_completion_tokens")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="retry"))],
            usage=None,
        )

    _azure_fake(monkeypatch, create_side_effect=_create, captured=captured)
    monkeypatch.setenv("TEST_API_KEY", "sk-x")
    monkeypatch.setenv("AZURE_ENDPOINT", "https://x.example")

    cfg = _mk_config(
        AIProvider.AZURE,
        model="gpt-4-turbo",  # starts with non-o-series → defaults to max_tokens
        azure_endpoint_env="AZURE_ENDPOINT",
        api_version="2024-02-01",
    )
    result = asyncio.run(AzureOpenAIClient(cfg).complete("sys", "user"))
    assert calls["n"] == 2
    assert captured[0].get("max_tokens") is not None
    assert "max_completion_tokens" not in captured[0]
    assert captured[1].get("max_completion_tokens") is not None
    assert "max_tokens" not in captured[1]
    assert result == "retry"


def test_azure_complete_unrelated_error_propagates(monkeypatch):
    import pytest

    async def _create(**kwargs):
        raise Exception("401 Unauthorized: invalid api key")

    _azure_fake(monkeypatch, create_side_effect=_create)
    monkeypatch.setenv("TEST_API_KEY", "sk-x")
    monkeypatch.setenv("AZURE_ENDPOINT", "https://x.example")

    cfg = _mk_config(
        AIProvider.AZURE,
        model="gpt-4-turbo",
        azure_endpoint_env="AZURE_ENDPOINT",
        api_version="2024-02-01",
    )
    with pytest.raises(Exception, match="401"):
        asyncio.run(AzureOpenAIClient(cfg).complete("sys", "user"))


# ---------------------------------------------------------------------------
# GeminiClient
# ---------------------------------------------------------------------------


def _gemini_fake(monkeypatch, *, generate_content_result=None, captured=None):
    if captured is None:
        captured = []

    async def _generate_content(**kwargs):
        captured.append(kwargs)
        return generate_content_result

    class _FakeModels:
        def __init__(self):
            self.generate_content = _generate_content

    class _FakeAio:
        def __init__(self):
            self.models = _FakeModels()

    class _FakeGenaiClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.aio = _FakeAio()

    monkeypatch.setattr("src.ai.client.genai.Client", _FakeGenaiClient)
    return captured


def test_gemini_complete_returns_text_and_records_usage(monkeypatch):
    record = _patch_record_usage(monkeypatch)
    captured = _gemini_fake(
        monkeypatch,
        generate_content_result=SimpleNamespace(
            text="hello",
            usage_metadata=SimpleNamespace(
                total_token_count=15, prompt_token_count=10, candidates_token_count=5
            ),
        ),
    )
    monkeypatch.setenv("TEST_API_KEY", "sk-gemini")
    cfg = _mk_config(AIProvider.GEMINI, model="gemini-test")
    result = asyncio.run(GeminiClient(cfg).complete("sys", "user"))
    assert result == "hello"
    # Verify the function created by ``types.GenerateContentConfig`` passed through
    assert captured[0]["model"] == "gemini-test"
    assert captured[0]["contents"] == "user"
    assert record == [{"provider": "gemini", "input_tokens": 10, "output_tokens": 5}]


def test_gemini_complete_handles_missing_usage_metadata(monkeypatch):
    record = _patch_record_usage(monkeypatch)
    _gemini_fake(
        monkeypatch,
        generate_content_result=SimpleNamespace(text="answer", usage_metadata=None),
    )
    monkeypatch.setenv("TEST_API_KEY", "sk-gemini")
    cfg = _mk_config(AIProvider.GEMINI)
    result = asyncio.run(GeminiClient(cfg).complete("sys", "user"))
    assert result == "answer"
    assert record == []


def test_gemini_complete_completion_never_negative(monkeypatch):
    """If total_token_count is missing but prompt_token_count is set, completion=0 (max(0, …))."""
    record = _patch_record_usage(monkeypatch)
    _gemini_fake(
        monkeypatch,
        generate_content_result=SimpleNamespace(
            text="hi",
            usage_metadata=SimpleNamespace(total_token_count=0, prompt_token_count=10),
        ),
    )
    monkeypatch.setenv("TEST_API_KEY", "sk-gemini")
    cfg = _mk_config(AIProvider.GEMINI)
    asyncio.run(GeminiClient(cfg).complete("sys", "user"))
    assert record == [{"provider": "gemini", "input_tokens": 10, "output_tokens": 0}]
