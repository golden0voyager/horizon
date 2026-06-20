"""Tests for src/ai/client.py module-level helpers.

Covers the pure-function layer at the top of src/ai/client.py:
- ``_resolve_api_key`` (env hit, fallback hit, ValueError paths)
- ``_missing_api_key_message`` (provider-specific message format + secret-value warning)
- ``_looks_like_api_key_value`` (secret prefix detection vs valid env var names)
- ``get_base_url`` (precedence: env > config > provider-env > default)

These functions have no side effects on external systems, so tests are
synchronous and exercise the function directly. The conftest.py autouse
fixture already strips HORIZON_AI_* env vars, so AIConfig.model_validator
won't silently override what we pass in.
"""

from src.ai.client import (
    _looks_like_api_key_value,
    _missing_api_key_message,
    _resolve_api_key,
    get_base_url,
)
from src.models import AIConfig, AIProvider


def _mk_config(
    provider: AIProvider = AIProvider.ANTHROPIC,
    *,
    api_key_env: str = "TEST_API_KEY",
    base_url: str | None = None,
    base_url_env: str | None = None,
) -> AIConfig:
    """Build an AIConfig without triggering provider-chain logic."""
    return AIConfig(
        provider=provider,
        model="test-model",
        api_key_env=api_key_env,
        base_url=base_url,
        base_url_env=base_url_env,
    )


# ---------------------------------------------------------------------------
# _resolve_api_key
# ---------------------------------------------------------------------------


def test_resolve_api_key_returns_env_value_when_set(monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "secret-value")
    assert _resolve_api_key(_mk_config()) == "secret-value"


def test_resolve_api_key_uses_fallback_when_env_missing(monkeypatch):
    monkeypatch.delenv("TEST_API_KEY", raising=False)
    assert _resolve_api_key(_mk_config(), fallback="fb") == "fb"


def test_resolve_api_key_raises_value_error_when_env_and_fallback_missing(monkeypatch):
    import pytest

    monkeypatch.delenv("TEST_API_KEY", raising=False)
    with pytest.raises(ValueError, match="api_key_env"):
        _resolve_api_key(_mk_config())


def test_resolve_api_key_empty_string_env_falls_through(monkeypatch):
    """``os.getenv`` returns ``None`` for unset names; an env set to "" remains falsy
    and triggers the fallback path, not an exception-at-resolve path."""
    import pytest

    monkeypatch.setenv("TEST_API_KEY", "")
    with pytest.raises(ValueError):
        _resolve_api_key(_mk_config())


# ---------------------------------------------------------------------------
# _missing_api_key_message
# ---------------------------------------------------------------------------


def test_missing_api_key_message_mentions_known_provider_default(monkeypatch):
    """For a recognised provider the message gives a concrete env var hint."""
    monkeypatch.delenv("TEST_API_KEY", raising=False)
    cfg = _mk_config(provider=AIProvider.ANTHROPIC)
    msg = _missing_api_key_message(cfg)
    assert "ANTHROPIC_API_KEY" in msg
    assert "Set" in msg


def test_missing_api_key_message_flags_when_api_key_env_looks_like_secret(monkeypatch):
    """If the user pasted an actual API key into api_key_env, the message tells them."""
    monkeypatch.delenv("MY_LITERAL_KEY", raising=False)
    cfg = _mk_config(api_key_env="sk-abc123")
    msg = _missing_api_key_message(cfg)
    assert "must be an environment variable name" in msg


def test_missing_api_key_message_falls_back_to_generic_for_unknown_provider(monkeypatch):
    """Provider not in ``_DEFAULT_API_KEY_ENVS`` → generic hint, no env var name."""
    monkeypatch.delenv("TEST_API_KEY", raising=False)
    cfg = _mk_config(provider=AIProvider.MODELSCOPE)  # not in _DEFAULT_API_KEY_ENVS
    msg = _missing_api_key_message(cfg)
    assert "Set the provider API key" in msg


# ---------------------------------------------------------------------------
# _looks_like_api_key_value
# ---------------------------------------------------------------------------


def test_looks_like_api_key_value_detects_all_documented_secret_prefixes():
    for prefix in ("sk-", "sk_", "AIza", "xai-", "gsk_", "hf_"):
        assert _looks_like_api_key_value(prefix + "rest") is True, prefix


def test_looks_like_api_key_value_accepts_valid_env_var_names():
    assert _looks_like_api_key_value("OPENAI_API_KEY") is False
    assert _looks_like_api_key_value("MY_CUSTOM_VAR_2") is False
    assert _looks_like_api_key_value("A") is False


def test_looks_like_api_key_value_rejects_strings_with_invalid_identifier_chars():
    assert _looks_like_api_key_value("my key value") is True
    assert _looks_like_api_key_value("1LEADING_DIGIT") is True
    assert _looks_like_api_key_value("") is True


# ---------------------------------------------------------------------------
# get_base_url
# ---------------------------------------------------------------------------


def test_get_base_url_base_url_env_takes_precedence(monkeypatch):
    monkeypatch.setenv("MY_BASE_URL_ENV", "https://from-env.example")
    cfg = _mk_config(
        base_url_env="MY_BASE_URL_ENV",
        base_url="https://from-config.example",
    )
    assert get_base_url(cfg) == "https://from-env.example"


def test_get_base_url_config_used_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("MY_BASE_URL_ENV", raising=False)
    cfg = _mk_config(
        base_url_env="MY_BASE_URL_ENV",
        base_url="https://from-config.example",
    )
    assert get_base_url(cfg) == "https://from-config.example"


def test_get_base_url_provider_named_env_var_used(monkeypatch):
    """``get_base_url`` falls through to ``<PROVIDER>_BASE_URL`` when neither
    ``base_url_env`` nor ``base_url`` yields a value."""
    monkeypatch.delenv("MY_BASE_URL_ENV", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openai-from-provider-env")
    cfg = _mk_config(
        provider=AIProvider.OPENAI,
        base_url_env="MY_BASE_URL_ENV",
    )
    assert get_base_url(cfg) == "https://openai-from-provider-env"


def test_get_base_url_returns_default_when_no_source_configured(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    cfg = _mk_config()
    assert get_base_url(cfg, default="https://onlydefault") == "https://onlydefault"


def test_get_base_url_returns_none_when_no_default_supplied(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    cfg = _mk_config()
    assert get_base_url(cfg) is None
