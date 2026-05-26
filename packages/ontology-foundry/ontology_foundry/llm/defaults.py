"""Default LLM provider settings — DeepSeek V3 via OpenAI-compatible API."""
from __future__ import annotations

import os

# LLM provider — DeepSeek V3 is the default
DEFAULT_CHAT_MODEL = "deepseek-chat"
DEFAULT_CHAT_BASE_URL = "https://api.deepseek.com"
DEFAULT_CHAT_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_CHAT_BASE_URL_ENV = "DEEPSEEK_BASE_URL"

# Embeddings (DeepSeek has no embedding API; use OpenAI or another provider)
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_API_KEY_ENV = "OPENAI_API_KEY"
DEFAULT_EMBEDDING_BASE_URL_ENV = "EMBEDDING_BASE_URL"
DEFAULT_EMBEDDING_BASE_URL = "https://api.openai.com/v1"


def get_chat_api_key() -> str | None:
    return os.environ.get(DEFAULT_CHAT_API_KEY_ENV) or None


def get_chat_base_url() -> str:
    raw = os.environ.get(DEFAULT_CHAT_BASE_URL_ENV, DEFAULT_CHAT_BASE_URL)
    return raw.rstrip("/")


def get_chat_model(override: str | None = None) -> str:
    if override:
        return override
    return os.environ.get("LLM_MODEL", DEFAULT_CHAT_MODEL)


def get_embedding_api_key() -> str | None:
    return (
        os.environ.get("EMBEDDING_API_KEY")
        or os.environ.get(DEFAULT_EMBEDDING_API_KEY_ENV)
        or None
    )


def get_embedding_base_url() -> str:
    raw = os.environ.get(DEFAULT_EMBEDDING_BASE_URL_ENV, DEFAULT_EMBEDDING_BASE_URL)
    return raw.rstrip("/")


def get_embedding_model(override: str | None = None) -> str:
    if override:
        return override
    return os.environ.get("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
