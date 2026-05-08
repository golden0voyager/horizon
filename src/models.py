"""Core data models for Horizon."""

from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any, Union
import os
from pydantic import BaseModel, HttpUrl, Field, model_validator


class SourceType(str, Enum):
    """Supported information source types."""
    GITHUB = "github"
    HACKERNEWS = "hackernews"
    RSS = "rss"
    REDDIT = "reddit"
    TELEGRAM = "telegram"
    TWITTER = "twitter"


class ContentItem(BaseModel):
    """Unified content item model from any source."""

    id: str  # Format: {source}:{subtype}:{native_id}
    source_type: SourceType
    title: str
    url: HttpUrl
    content: Optional[str] = None
    author: Optional[str] = None
    published_at: datetime
    fetched_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    # AI analysis results
    ai_score: Optional[float] = None  # 0-10 importance score
    ai_reason: Optional[str] = None
    ai_summary: Optional[str] = None
    ai_tags: List[str] = Field(default_factory=list)


class AIProvider(str, Enum):
    """Supported AI providers."""
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    AZURE = "azure"
    ALI = "ali"
    GEMINI = "gemini"
    DOUBAO = "doubao"
    MINIMAX = "minimax"
    DEEPSEEK = "deepseek"
    MODELSCOPE = "modelscope"
    XIAOMIMIMO = "xiaomimimo"
    MOONSHOTAI = "moonshotai"
    OPENROUTER = "openrouter"
    GROQ = "groq"
    SILICONFLOW = "siliconflow"
    NVIDIA = "nvidia"

# Default models and API key env vars for each provider
AI_PROVIDER_DEFAULTS = {
    AIProvider.ALI: {
        "model": "qwen3.5-plus-2026-02-15",
        "api_key_env": "DASHSCOPE_API_KEY",
    },
    AIProvider.OPENROUTER: {
        "model": "tencent/hy3-preview:free",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    AIProvider.GROQ: {
        "model": "llama-3.1-8b-instant",
        "api_key_env": "GROQ_API_KEY",
    },
    AIProvider.DEEPSEEK: {
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    AIProvider.GEMINI: {
        "model": "gemini-3-flash-preview",
        "api_key_env": "GOOGLE_API_KEY",
    },
    AIProvider.SILICONFLOW: {
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "api_key_env": "SILICONFLOW_API_KEY",
    },
    AIProvider.ANTHROPIC: {
        "model": "claude-3-5-sonnet-20241022",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    AIProvider.XIAOMIMIMO: {
        "model": "mimo-v2.5-pro",
        "api_key_env": "XIAOMIMIMO_API_KEY",
    }
}


class AIConfig(BaseModel):
    """AI client configuration."""

    provider: AIProvider
    model: str
    base_url: Optional[str] = None
    base_url_env: Optional[str] = None
    api_key_env: str
    temperature: float = 0.3
    max_tokens: int = 4096
    throttle_sec: float = 0.0
    languages: List[str] = Field(default_factory=lambda: ["en"])
    # Azure OpenAI specific; required when provider == AZURE
    azure_endpoint_env: Optional[str] = None
    api_version: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def override_from_env(cls, data: Any) -> Any:
        """Allow environment variables to override JSON config."""
        if isinstance(data, dict):
            provider_env = os.getenv("HORIZON_AI_PROVIDER")
            if provider_env:
                try:
                    provider = AIProvider(provider_env)
                    data["provider"] = provider
                    
                    # Apply smart defaults for the chosen provider if not explicitly overridden
                    if provider in AI_PROVIDER_DEFAULTS:
                        defaults = AI_PROVIDER_DEFAULTS[provider]
                        
                        # Only apply default model if not provided in env OR current data
                        if not os.getenv("HORIZON_AI_MODEL"):
                            data["model"] = defaults["model"]
                            
                        # Only apply default key env if not provided in env OR current data
                        if not os.getenv("HORIZON_AI_API_KEY_ENV"):
                            data["api_key_env"] = defaults["api_key_env"]
                except ValueError:
                    # If invalid provider string, fallback to original logic or ignore
                    data["provider"] = provider_env

            # Explicit overrides still take ultimate precedence
            model_env = os.getenv("HORIZON_AI_MODEL")
            if model_env:
                data["model"] = model_env
            api_key_env = os.getenv("HORIZON_AI_API_KEY_ENV")
            if api_key_env:
                data["api_key_env"] = api_key_env
        return data


class GitHubSourceConfig(BaseModel):
    """GitHub source configuration."""

    type: str  # "user_events", "repo_releases", etc.
    username: Optional[str] = None
    owner: Optional[str] = None
    repo: Optional[str] = None
    enabled: bool = True


class HackerNewsConfig(BaseModel):
    """Hacker News configuration."""

    enabled: bool = True
    fetch_top_stories: int = 30
    min_score: int = 100


class RSSSourceConfig(BaseModel):
    """RSS feed source configuration."""

    name: str
    url: HttpUrl
    enabled: bool = True
    category: Optional[str] = None


class RedditSubredditConfig(BaseModel):
    """Configuration for monitoring a specific subreddit."""
    subreddit: str
    enabled: bool = True
    sort: str = "hot"           # hot, new, top, rising
    time_filter: str = "day"    # hour, day, week, month, year, all (only for top/controversial)
    fetch_limit: int = 25
    min_score: int = 10


class RedditUserConfig(BaseModel):
    """Configuration for monitoring a specific Reddit user."""
    username: str               # without u/ prefix
    enabled: bool = True
    sort: str = "new"
    fetch_limit: int = 10


class RedditConfig(BaseModel):
    """Reddit source configuration."""
    enabled: bool = True
    subreddits: List[RedditSubredditConfig] = Field(default_factory=list)
    users: List[RedditUserConfig] = Field(default_factory=list)
    fetch_comments: int = 5     # top comments per post, 0 to disable


class TelegramChannelConfig(BaseModel):
    """Configuration for monitoring a specific Telegram channel."""
    channel: str            # channel username, e.g. "zaihuapd"
    enabled: bool = True
    fetch_limit: int = 20


class TelegramConfig(BaseModel):
    """Telegram source configuration."""
    enabled: bool = True
    channels: List[TelegramChannelConfig] = Field(default_factory=list)


class TwitterConfig(BaseModel):
    """Twitter source configuration via Apify."""
    enabled: bool = True
    apify_token_env: str = "APIFY_TOKEN"
    actor_id: str = "altimis~scweet"
    users: List[str] = Field(default_factory=list)
    fetch_limit: int = 10
    fetch_reply_text: bool = False
    max_replies_per_tweet: int = 3
    max_tweets_to_expand: int = 10
    reply_min_likes: int = 0


class SourcesConfig(BaseModel):
    """All sources configuration."""

    github: List[GitHubSourceConfig] = Field(default_factory=list)
    hackernews: HackerNewsConfig = Field(default_factory=HackerNewsConfig)
    rss: List[RSSSourceConfig] = Field(default_factory=list)
    reddit: RedditConfig = Field(default_factory=RedditConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    twitter: Optional[TwitterConfig] = None


class WebhookConfig(BaseModel):
    """Webhook notification configuration."""

    url_env: Optional[str] = None          # Environment variable name containing the webhook URL
    request_body: Optional[Union[str, dict, list]] = None  # POST body: real JSON object or string with #{key} placeholders; if empty, will use GET
    headers: Optional[str] = None          # Custom headers, "Key: Value" per line
    delivery: str = "summary"             # summary, or summary_and_items
    overview_position: str = "first"       # For summary_and_items: first, or last
    platform: str = "generic"              # generic, feishu, lark, dingtalk, slack, discord
    layout: str = "markdown"               # markdown, or collapsible
    fallback_layout: str = "markdown"      # Layout to use when the requested layout is unsupported
    languages: Optional[List[str]] = None  # Optional language filter for webhook delivery; defaults to all AI languages
    enabled: bool = False


class EmailConfig(BaseModel):
    """Email configuration for updates/subscriptions."""
    imap_server: str
    imap_port: int = 993
    smtp_server: str
    smtp_port: int = 465
    email_address: str
    password_env: str = "EMAIL_PASSWORD"
    sender_name: str = "Horizon Daily"
    subscribe_keyword: str = "SUBSCRIBE"
    unsubscribe_keyword: str = "UNSUBSCRIBE"
    enabled: bool = False


class FilteringConfig(BaseModel):
    """Content filtering configuration."""

    ai_score_threshold: float = 7.0
    time_window_hours: int = 24


class Config(BaseModel):
    """Main configuration model."""

    version: str = "1.0"
    ai: AIConfig
    sources: SourcesConfig
    filtering: FilteringConfig
    email: Optional[EmailConfig] = None
    webhook: Optional[WebhookConfig] = None
