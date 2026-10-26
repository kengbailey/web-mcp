"""Runtime settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, "").strip()
    return value or default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except ValueError:
        return default


def _proxy(name: str, inherit: str | None) -> str | None:
    """Unset inherits PROXY_URL; set-but-empty forces a direct connection."""
    return _env(name) if name in os.environ else inherit


@dataclass(frozen=True)
class Settings:
    exa_api_key: str | None = None
    youtube_api_key: str | None = None
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "python:web-mcp:v3"
    proxy_url: str | None = None
    reddit_proxy_url: str | None = None
    youtube_proxy_url: str | None = None
    stt_endpoint: str = "https://openrouter.ai/api/v1"
    stt_model: str = "nvidia/parakeet-tdt-0.6b-v3"
    stt_fallback_model: str = "openai/whisper-large-v3-turbo"
    stt_api_key: str | None = None
    stt_max_seconds: int = 4 * 3600
    default_max_chars: int = 8000
    browser_prewarm: bool = False
    user_agent: str = USER_AGENT

    @classmethod
    def from_env(cls) -> "Settings":
        proxy = _env("PROXY_URL")
        return cls(
            exa_api_key=_env("EXA_API_KEY"),
            youtube_api_key=_env("YOUTUBE_API_KEY"),
            reddit_client_id=_env("REDDIT_CLIENT_ID"),
            reddit_client_secret=_env("REDDIT_CLIENT_SECRET"),
            reddit_user_agent=_env("REDDIT_USER_AGENT", cls.reddit_user_agent),
            proxy_url=proxy,
            # Reddit blocks many VPN exits, so it does not inherit PROXY_URL.
            reddit_proxy_url=_env("REDDIT_PROXY_URL"),
            # YouTube bot-checks datacenter/VPN IPs; it inherits unless overridden.
            youtube_proxy_url=_proxy("YOUTUBE_PROXY_URL", proxy),
            stt_endpoint=_env("STT_ENDPOINT", cls.stt_endpoint).rstrip("/"),
            stt_model=_env("STT_MODEL", cls.stt_model),
            stt_fallback_model=_env("STT_FALLBACK_MODEL", cls.stt_fallback_model),
            stt_api_key=_env("STT_API_KEY") or _env("OPENROUTER_API_KEY"),
            stt_max_seconds=_int("STT_MAX_SECONDS", cls.stt_max_seconds),
            default_max_chars=min(max(_int("DEFAULT_MAX_CHARS", 8000), 500), 20000),
            browser_prewarm=_env("BROWSER_PREWARM", "0").lower() in ("1", "true", "yes"),
        )
