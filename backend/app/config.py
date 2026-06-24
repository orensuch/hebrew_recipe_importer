"""Runtime configuration, all overridable via environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # Base URL of the Ollama server. When running in Docker against the host's
    # Ollama, this is http://host.docker.internal:11434 (wired in compose).
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434").rstrip("/")
    # Model tag that must already be pulled in that Ollama instance.
    ollama_model: str = os.getenv("OLLAMA_MODEL", "gemma2:9b")
    # Context window hint passed to Ollama (num_ctx).
    ollama_num_ctx: int = _int("OLLAMA_NUM_CTX", 8192)
    # How long to wait on the model. Parsing a long page can take a while.
    ollama_timeout: int = _int("OLLAMA_TIMEOUT", 300)
    # Timeout for fetching the recipe page itself.
    fetch_timeout: int = _int("FETCH_TIMEOUT", 30)
    # Cap the scraped text we feed the model so prompts stay bounded.
    max_content_chars: int = _int("MAX_CONTENT_CHARS", 14000)

    # --- Mealie -------------------------------------------------------------
    # URL the BACKEND uses to reach Mealie (same host -> host.docker.internal).
    mealie_url: str = os.getenv("MEALIE_URL", "http://host.docker.internal:1189").rstrip("/")
    # URL the BROWSER uses for links back to Mealie (must be host-reachable).
    mealie_public_url: str = os.getenv("MEALIE_PUBLIC_URL", "http://localhost:1189").rstrip("/")
    # Long-lived API token from Mealie: Settings -> API Tokens. Empty = disabled.
    mealie_token: str = os.getenv("MEALIE_API_TOKEN", "")
    # Recipe group slug used when building the browser link (Mealie 3.x).
    mealie_group: str = os.getenv("MEALIE_GROUP", "home")
    mealie_timeout: int = _int("MEALIE_TIMEOUT", 60)


settings = Settings()
