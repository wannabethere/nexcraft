"""LangChain helpers defaulting to DeepSeek for chat (OpenAI-compatible API)."""
from __future__ import annotations

from typing import Any

from ontology_foundry.llm.defaults import (
    DEFAULT_CHAT_API_KEY_ENV,
    get_chat_api_key,
    get_chat_base_url,
    get_chat_model,
    get_embedding_api_key,
    get_embedding_base_url,
    get_embedding_model,
)


def create_chat_openai(
    *,
    temperature: float = 0.0,
    model: str | None = None,
    **kwargs: Any,
) -> Any:
    """Build langchain_openai.ChatOpenAI pointed at DeepSeek by default."""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise ImportError(
            "create_chat_openai requires langchain-openai. "
            "Install with ontology-foundry[langchain] or ontology-store[vector]."
        ) from exc

    api_key = kwargs.pop("openai_api_key", None) or get_chat_api_key()
    if not api_key:
        raise RuntimeError(
            f"{DEFAULT_CHAT_API_KEY_ENV} must be set for LangChain chat (DeepSeek). "
            "Example: export DEEPSEEK_API_KEY=..."
        )

    base_url = kwargs.pop("base_url", None) or get_chat_base_url()
    return ChatOpenAI(
        model=model or get_chat_model(),
        temperature=temperature,
        openai_api_key=api_key,
        base_url=base_url,
        **kwargs,
    )


def create_openai_embeddings(
    *,
    model: str | None = None,
    **kwargs: Any,
) -> Any:
    """Build langchain_openai.OpenAIEmbeddings (OpenAI or custom base URL)."""
    try:
        from langchain_openai import OpenAIEmbeddings
    except ImportError as exc:
        raise ImportError(
            "create_openai_embeddings requires langchain-openai. "
            "Install with ontology-store[vector]."
        ) from exc

    api_key = kwargs.pop("openai_api_key", None) or get_embedding_api_key()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY or EMBEDDING_API_KEY must be set for embeddings "
            "(DeepSeek does not expose an embedding API)."
        )

    base_url = kwargs.pop("base_url", None) or get_embedding_base_url()
    return OpenAIEmbeddings(
        model=model or get_embedding_model(),
        openai_api_key=api_key,
        base_url=base_url,
        **kwargs,
    )
