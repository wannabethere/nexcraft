"""Embedder abstraction.

`Embedder` is the Protocol used by the store + indexer; `OpenAIEmbedder` is
the default backed by langchain_openai's `OpenAIEmbeddings`. Concrete embedders
can be swapped for any provider that supports embed_query / embed_documents.
"""
from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    """Minimal embedder contract."""

    def embed_query(self, text: str) -> list[float]: ...
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    @property
    def dim(self) -> int: ...


class OpenAIEmbedder:
    """OpenAI-compatible embeddings via langchain_openai.

    DeepSeek has no embedding API — set OPENAI_API_KEY (or EMBEDDING_API_KEY) for vectors.
    Chat LLMs use DEEPSEEK_API_KEY via ontology_foundry.llm.defaults.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        dim_override: int | None = None,
    ) -> None:
        try:
            from ontology_foundry.llm.langchain_client import create_openai_embeddings
        except ImportError as exc:
            raise ImportError(
                "OpenAIEmbedder requires ontology-foundry and langchain-openai. "
                "Install with 'ontology-store[vector]'."
            ) from exc

        from ontology_foundry.llm.defaults import get_embedding_model

        resolved_model = model or get_embedding_model()
        self._inner = create_openai_embeddings(
            model=resolved_model,
            openai_api_key=api_key,
            base_url=base_url,
        )
        self._model_name = resolved_model
        self._dim = dim_override if dim_override is not None else _DEFAULT_DIMS.get(
            self._model_name, 1536
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dim(self) -> int:
        return self._dim

    def embed_query(self, text: str) -> list[float]:
        return self._inner.embed_query(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._inner.embed_documents(texts)


_DEFAULT_DIMS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}
