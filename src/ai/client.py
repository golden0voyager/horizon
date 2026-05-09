"""AI client abstraction supporting multiple providers."""

import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI, AsyncAzureOpenAI
from google import genai
from google.genai import types

from ..models import AIConfig, AIProvider
from rich import print as rich_print

from .tokens import record_usage


def get_base_url(config: AIConfig, default: Optional[str] = None) -> Optional[str]:
    """Resolve base URL from env, config, or default."""
    if config.base_url_env:
        env_url = os.getenv(config.base_url_env)
        if env_url:
            return env_url
    if config.base_url:
        return config.base_url
        
    # Fallback to provider-specific base URL from .env if defined
    provider_env_var = f"{config.provider.name.upper()}_BASE_URL"
    provider_env_url = os.getenv(provider_env_var)
    if provider_env_url:
        return provider_env_url
        
    return default


class AIClient(ABC):
    """Abstract base class for AI clients."""

    @abstractmethod
    async def complete(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate completion from AI model.

        Args:
            system: System prompt
            user: User prompt
            temperature: Optional sampling temperature override
            max_tokens: Optional maximum tokens override

        Returns:
            str: Generated completion text
        """
        pass


class AnthropicClient(AIClient):
    """Client for Anthropic Claude models."""

    def __init__(self, config: AIConfig):
        """Initialize Anthropic client.

        Args:
            config: AI configuration
        """
        self.config = config

        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise ValueError(f"Missing API key: {config.api_key_env}")

        kwargs = {"api_key": api_key}
        base_url = get_base_url(config)
        if base_url:
            kwargs["base_url"] = base_url

        self.client = AsyncAnthropic(**kwargs)
        self.model = config.model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens

    async def complete(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate completion using Claude.

        Args:
            system: System prompt
            user: User prompt
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate

        Returns:
            str: Generated text
        """
        temperature = self.temperature if temperature is None else temperature
        max_tokens = self.max_tokens if max_tokens is None else max_tokens

        message = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}]
        )
        usage = getattr(message, "usage", None)
        if usage is not None:
            record_usage(
                "anthropic",
                input_tokens=getattr(usage, "input_tokens", 0),
                output_tokens=getattr(usage, "output_tokens", 0),
            )
        return message.content[0].text


class OpenAIClient(AIClient):
    """Client for OpenAI models."""

    def __init__(self, config: AIConfig):
        """Initialize OpenAI client.

        Args:
            config: AI configuration
        """
        self.config = config

        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise ValueError(f"Missing API key: {config.api_key_env}")

        kwargs = {"api_key": api_key}
        base_url = get_base_url(config)
        if base_url:
            kwargs["base_url"] = base_url

        self.client = AsyncOpenAI(**kwargs)
        self.model = config.model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens

    async def complete(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate completion using OpenAI.

        Args:
            system: System prompt
            user: User prompt
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate

        Returns:
            str: Generated text
        """
        temperature = self.temperature if temperature is None else temperature
        max_tokens = self.max_tokens if max_tokens is None else max_tokens

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"}
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            record_usage(
                "openai",
                input_tokens=getattr(usage, "prompt_tokens", 0),
                output_tokens=getattr(usage, "completion_tokens", 0),
            )
        return response.choices[0].message.content


class AzureOpenAIClient(AIClient):
    """Client for Azure OpenAI deployments.

    Uses the native AsyncAzureOpenAI client, which requires the deployment
    name (passed as `model`), azure_endpoint (resource base URL), and
    api_version. The deployment path is assembled internally by the SDK.
    """

    # Newer reasoning-series models reject legacy `max_tokens` and require
    # `max_completion_tokens` instead. Azure uses deployment names as `model`,
    # so a best-effort guess can be wrong for custom deployment aliases.
    _MODELS_REQUIRING_MAX_COMPLETION_TOKENS = ("o1", "o3", "o4", "gpt-5")

    def __init__(self, config: AIConfig):
        """Initialize Azure OpenAI client.

        Args:
            config: AI configuration
        """
        self.config = config

        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise ValueError(f"Missing API key: {config.api_key_env}")
        if not config.azure_endpoint_env:
            raise ValueError("azure_endpoint_env is required for azure provider")
        azure_endpoint = os.getenv(config.azure_endpoint_env)
        if not azure_endpoint:
            raise ValueError(f"Missing Azure endpoint: {config.azure_endpoint_env}")
        if not config.api_version:
            raise ValueError("api_version is required for azure provider")

        self.client = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=azure_endpoint,
            api_version=config.api_version,
        )
        self.model = config.model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens
        self._use_max_completion_tokens = any(
            config.model.startswith(prefix)
            for prefix in self._MODELS_REQUIRING_MAX_COMPLETION_TOKENS
        )

    async def complete(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate completion using Azure OpenAI.

        Args:
            system: System prompt
            user: User prompt
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate

        Returns:
            str: Generated text
        """
        temperature = self.temperature if temperature is None else temperature
        max_tokens = self.max_tokens if max_tokens is None else max_tokens

        try:
            response = await self._create_completion(
                system=system,
                user=user,
                temperature=temperature,
                max_tokens=max_tokens,
                use_max_completion_tokens=self._use_max_completion_tokens,
            )
        except Exception as exc:
            fallback = self._token_fallback_mode(str(exc))
            if fallback is None:
                raise

            self._use_max_completion_tokens = fallback
            response = await self._create_completion(
                system=system,
                user=user,
                temperature=temperature,
                max_tokens=max_tokens,
                use_max_completion_tokens=fallback,
            )

        usage = getattr(response, "usage", None)
        if usage is not None:
            record_usage(
                "openai",
                input_tokens=getattr(usage, "prompt_tokens", 0),
                output_tokens=getattr(usage, "completion_tokens", 0),
            )
        return response.choices[0].message.content

    async def _create_completion(
        self,
        *,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        use_max_completion_tokens: bool,
    ):
        tokens_kwarg = (
            {"max_completion_tokens": max_tokens}
            if use_max_completion_tokens
            else {"max_tokens": max_tokens}
        )
        return await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            response_format={"type": "json_object"},
            **tokens_kwarg,
        )

    @staticmethod
    def _token_fallback_mode(message: str) -> Optional[bool]:
        lowered = message.lower()
        if "max_completion_tokens" in lowered and "max_tokens" in lowered:
            return True
        if "max_tokens" in lowered and "max_completion_tokens" not in lowered:
            return False
        return None


class MiniMaxClient(AIClient):
    """Client for MiniMax models via OpenAI-compatible API."""

    def __init__(self, config: AIConfig):
        """Initialize MiniMax client.

        Args:
            config: AI configuration
        """
        self.config = config

        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise ValueError(f"Missing API key: {config.api_key_env}")

        kwargs = {
            "api_key": api_key,
            "base_url": get_base_url(config, "https://api.minimax.io/v1"),
        }

        self.client = AsyncOpenAI(**kwargs)
        self.model = config.model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens

    async def complete(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate completion using MiniMax.

        MiniMax requires temperature in (0.0, 1.0] and does not support
        response_format, so we rely on prompt engineering for JSON output.

        Args:
            system: System prompt
            user: User prompt
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate

        Returns:
            str: Generated text
        """
        temperature = self.temperature if temperature is None else temperature
        max_tokens = self.max_tokens if max_tokens is None else max_tokens

        # MiniMax temperature must be in (0.0, 1.0]; clamp 0 to a small value
        if temperature <= 0:
            temperature = 0.01

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            record_usage(
                "minimax",
                input_tokens=getattr(usage, "prompt_tokens", 0),
                output_tokens=getattr(usage, "completion_tokens", 0),
            )
        return response.choices[0].message.content


class AliClient(AIClient):
    """Client for Alibaba DashScope (OpenAI-compatible API)."""

    def __init__(self, config: AIConfig):
        """Initialize DashScope client.

        Args:
            config: AI configuration
        """
        self.config = config

        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise ValueError(f"Missing API key: {config.api_key_env}")

        kwargs = {
            "api_key": api_key,
            "base_url": get_base_url(config, "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        }
        self.client = AsyncOpenAI(**kwargs)
        self.model = config.model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens

    async def complete(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate completion using DashScope.

        Args:
            system: System prompt
            user: User prompt
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate

        Returns:
            str: Generated text
        """
        temperature = self.temperature if temperature is None else temperature
        max_tokens = self.max_tokens if max_tokens is None else max_tokens

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"}
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            record_usage(
                self.config.provider.value,
                input_tokens=getattr(usage, "prompt_tokens", 0),
                output_tokens=getattr(usage, "completion_tokens", 0),
            )
        return response.choices[0].message.content


class GeminiClient(AIClient):
    """Client for Google Gemini models."""

    def __init__(self, config: AIConfig):
        """Initialize Gemini client.

        Args:
            config: AI configuration
        """
        self.config = config

        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise ValueError(f"Missing API key: {config.api_key_env}")

        self.client = genai.Client(api_key=api_key)
        self.model = config.model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens

    async def complete(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate completion using Gemini with detailed error logging."""
        temperature = self.temperature if temperature is None else temperature
        max_tokens = self.max_tokens if max_tokens is None else max_tokens

        try:
            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                    response_mime_type="application/json"
                )
            )
            usage = getattr(response, "usage_metadata", None)
            if usage is not None:
                total = getattr(usage, "total_token_count", 0) or 0
                prompt = getattr(usage, "prompt_token_count", 0) or 0
                completion = max(0, total - prompt)
                record_usage("gemini", input_tokens=prompt, output_tokens=completion)
            return response.text
        except Exception as e:
            print(f"\n❌ Gemini API Error ({self.model}): {str(e)}")
            raise e


class DeepSeekClient(AliClient):
    """Client for DeepSeek API."""
    def __init__(self, config: AIConfig):
        super().__init__(config)
        # Override the base_url set by AliClient if not explicitly provided
        api_key = os.getenv(config.api_key_env)
        kwargs = {
            "api_key": api_key,
            "base_url": get_base_url(config, "https://api.deepseek.com"),
        }
        self.client = AsyncOpenAI(**kwargs)


class ModelScopeClient(AliClient):
    """Client for ModelScope API."""
    def __init__(self, config: AIConfig):
        super().__init__(config)
        api_key = os.getenv(config.api_key_env)
        kwargs = {
            "api_key": api_key,
            "base_url": get_base_url(config, "https://api-inference.modelscope.cn/v1"),
        }
        self.client = AsyncOpenAI(**kwargs)


class XiaomiMiMoClient(AliClient):
    """Client for Xiaomi MiMo API."""
    def __init__(self, config: AIConfig):
        super().__init__(config)
        api_key = os.getenv(config.api_key_env)
        kwargs = {
            "api_key": api_key,
            "base_url": get_base_url(config, "https://mimo.xiaomi.com/api/v1"),
        }
        self.client = AsyncOpenAI(**kwargs)


class MoonshotClient(AliClient):
    """Client for Moonshot AI (Kimi) API."""
    def __init__(self, config: AIConfig):
        super().__init__(config)
        api_key = os.getenv(config.api_key_env)
        kwargs = {
            "api_key": api_key,
            "base_url": get_base_url(config, "https://api.moonshot.cn/v1"),
        }
        self.client = AsyncOpenAI(**kwargs)


class OpenRouterClient(AliClient):
    """Client for OpenRouter API."""
    def __init__(self, config: AIConfig):
        super().__init__(config)
        api_key = os.getenv(config.api_key_env)
        base_url = get_base_url(config, "https://openrouter.ai/api/v1")
        # print(f"DEBUG: Initializing OpenRouter with URL: {base_url} and Key: {api_key[:10] if api_key else 'None'}****")
        kwargs = {
            "api_key": api_key,
            "base_url": base_url,
            "default_headers": {
                "HTTP-Referer": "https://github.com/Golden0Voyager/Horizon",
                "X-Title": "Horizon AI Aggregator",
            }
        }
        self.client = AsyncOpenAI(**kwargs)
        self.model = config.model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens

    async def complete(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate completion without response_format (fixes 400 errors on some providers)."""
        temperature = self.temperature if temperature is None else temperature
        max_tokens = self.max_tokens if max_tokens is None else max_tokens

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            # 去掉 response_format，解决部分 OpenRouter 模型的兼容性问题
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            record_usage(
                "openrouter",
                input_tokens=getattr(usage, "prompt_tokens", 0),
                output_tokens=getattr(usage, "completion_tokens", 0),
            )
        return response.choices[0].message.content


class SenseNovaClient(AliClient):
    """Client for SenseNova (商汤) API."""

    def __init__(self, config: AIConfig):
        super().__init__(config)
        api_key = os.getenv(config.api_key_env)
        kwargs = {
            "api_key": api_key,
            "base_url": get_base_url(config, "https://api.sensenova.cn/compatible-mode/v2"),
        }
        self.client = AsyncOpenAI(**kwargs)


class GroqClient(AliClient):
    """Client for Groq API."""
    def __init__(self, config: AIConfig):
        super().__init__(config)
        api_key = os.getenv(config.api_key_env)
        kwargs = {
            "api_key": api_key,
            "base_url": get_base_url(config, "https://api.groq.com/openai/v1"),
        }
        self.client = AsyncOpenAI(**kwargs)
        self.model = config.model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens

    async def complete(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate completion without response_format for Groq with error logging."""
        temperature = self.temperature if temperature is None else temperature
        max_tokens = self.max_tokens if max_tokens is None else max_tokens

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user}
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            usage = getattr(response, "usage", None)
            if usage is not None:
                record_usage(
                    "groq",
                    input_tokens=getattr(usage, "prompt_tokens", 0),
                    output_tokens=getattr(usage, "completion_tokens", 0),
                )
            return response.choices[0].message.content
        except Exception as e:
            raise e



class SiliconFlowClient(AliClient):
    """Client for SiliconFlow API."""
    def __init__(self, config: AIConfig):
        super().__init__(config)
        api_key = os.getenv(config.api_key_env)
        kwargs = {
            "api_key": api_key,
            "base_url": get_base_url(config, "https://api.siliconflow.cn/v1"),
        }
        self.client = AsyncOpenAI(**kwargs)
        self.model = config.model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens

    async def complete(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate completion without response_format for SiliconFlow."""
        temperature = self.temperature if temperature is None else temperature
        max_tokens = self.max_tokens if max_tokens is None else max_tokens

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            record_usage(
                "siliconflow",
                input_tokens=getattr(usage, "prompt_tokens", 0),
                output_tokens=getattr(usage, "completion_tokens", 0),
            )
        return response.choices[0].message.content



class NvidiaClient(AliClient):
    """Client for Nvidia API."""
    def __init__(self, config: AIConfig):
        super().__init__(config)
        api_key = os.getenv(config.api_key_env)
        kwargs = {
            "api_key": api_key,
            "base_url": get_base_url(config, "https://integrate.api.nvidia.com/v1"),
        }
        self.client = AsyncOpenAI(**kwargs)


class ChainedAIClient(AIClient):
    """Chain multiple AI clients with automatic fallback.

    When a provider fails with a retryable error (rate limit, auth/quota,
    service unavailable, or empty response), automatically falls back to
    the next provider in the chain.

    Clients are created lazily so that missing API keys for downstream
    providers do not block startup when the primary provider works.
    """

    def __init__(
        self,
        configs: List[AIConfig],
        clients: Optional[List[AIClient]] = None,
        client_factory: Optional[Any] = None,
    ):
        self.configs = configs
        self._client_factory = client_factory or _create_single_client
        self._client_cache: Dict[int, AIClient] = {}
        # Allow tests to inject pre-built clients directly
        if clients is not None:
            for idx, client in enumerate(clients):
                self._client_cache[idx] = client

    def _get_client(self, index: int) -> AIClient:
        if index not in self._client_cache:
            self._client_cache[index] = self._client_factory(self.configs[index])
        return self._client_cache[index]

    async def complete(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        last_error: Optional[Exception] = None
        for i in range(len(self.configs)):
            try:
                client = self._get_client(i)
                result = await client.complete(system, user, temperature, max_tokens)
                if not result or not result.strip():
                    raise ValueError("Empty response from provider")
                return result
            except Exception as exc:
                if not self._should_fallback(exc):
                    raise
                last_error = exc
                if i < len(self.configs) - 1:
                    rich_print(
                        f"\n[yellow]Provider {self.configs[i].provider.value} failed ({exc}), "
                        f"falling back to {self.configs[i + 1].provider.value}...[/yellow]"
                    )
        raise RuntimeError(f"All providers failed. Last error: {last_error}")

    @staticmethod
    def _should_fallback(exc: Exception) -> bool:
        """Determine if an error warrants fallback to the next provider."""
        msg = str(exc).lower()
        if "429" in msg or "rate limit" in msg:
            return True
        if "401" in msg or "403" in msg or "quota" in msg or "exceeded" in msg:
            return True
        if "502" in msg or "503" in msg or "service unavailable" in msg:
            return True
        if "empty response" in msg:
            return True
        return False


def _create_single_client(config: AIConfig) -> AIClient:
    """Create a single AI client instance."""
    if config.provider == AIProvider.ANTHROPIC:
        return AnthropicClient(config)
    elif config.provider == AIProvider.OPENAI:
        return OpenAIClient(config)
    elif config.provider == AIProvider.AZURE:
        return AzureOpenAIClient(config)
    elif config.provider == AIProvider.ALI:
        return AliClient(config)
    elif config.provider == AIProvider.GEMINI:
        return GeminiClient(config)
    elif config.provider == AIProvider.DOUBAO:
        return OpenAIClient(config)
    elif config.provider == AIProvider.MINIMAX:
        return MiniMaxClient(config)
    elif config.provider == AIProvider.DEEPSEEK:
        return DeepSeekClient(config)
    elif config.provider == AIProvider.MODELSCOPE:
        return ModelScopeClient(config)
    elif config.provider == AIProvider.XIAOMIMIMO:
        return XiaomiMiMoClient(config)
    elif config.provider == AIProvider.MOONSHOTAI:
        return MoonshotClient(config)
    elif config.provider == AIProvider.OPENROUTER:
        return OpenRouterClient(config)
    elif config.provider == AIProvider.GROQ:
        return GroqClient(config)
    elif config.provider == AIProvider.SILICONFLOW:
        return SiliconFlowClient(config)
    elif config.provider == AIProvider.NVIDIA:
        return NvidiaClient(config)
    elif config.provider == AIProvider.SENSENOVA:
        return SenseNovaClient(config)
    else:
        raise ValueError(f"Unsupported AI provider: {config.provider}")


def _create_chained_client(config: AIConfig) -> ChainedAIClient:
    """Build a ChainedAIClient from a comma-separated provider chain."""
    from ..models import AI_PROVIDER_DEFAULTS

    provider_names = [p.strip() for p in config.provider_chain.split(",") if p.strip()]
    if not provider_names:
        raise ValueError("provider_chain is empty")

    chain_configs: List[AIConfig] = []
    for name in provider_names:
        try:
            provider = AIProvider(name)
        except ValueError:
            raise ValueError(f"Unsupported AI provider in chain: {name}")

        defaults = AI_PROVIDER_DEFAULTS.get(provider, {})
        cfg = AIConfig(
            provider=provider,
            model=defaults.get("model", config.model),
            api_key_env=defaults.get("api_key_env", config.api_key_env),
            base_url=config.base_url,
            base_url_env=config.base_url_env,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            throttle_sec=config.throttle_sec,
            languages=config.languages,
            azure_endpoint_env=config.azure_endpoint_env,
            api_version=config.api_version,
        )
        chain_configs.append(cfg)

    return ChainedAIClient(chain_configs)


def create_ai_client(config: AIConfig) -> AIClient:
    """Factory function to create appropriate AI client.

    Args:
        config: AI configuration

    Returns:
        AIClient: Initialized AI client

    Raises:
        ValueError: If provider is not supported
    """
    if config.provider_chain:
        return _create_chained_client(config)
    return _create_single_client(config)
