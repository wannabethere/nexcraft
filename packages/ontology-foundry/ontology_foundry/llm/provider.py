from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel


class ModelRole(StrEnum):
    """
    Logical roles mapped to deployment-specific models (extraction §4.5, §6.2 context.llm).
    """

    CLAIM_EXTRACTOR_STRONG = "claim_extractor_strong"
    CLAIM_EXTRACTOR_DEFAULT = "claim_extractor_default"
    RELATION_EXTRACTOR = "relation_extractor"
    PREDICATE_CANONICALIZER = "predicate_canonicalizer"
    VALIDATOR = "validator"
    SUMMARIZER = "summarizer"
    EMBEDDING_DEFAULT = "embedding_default"


class ModelProvider(Protocol):
    """Pluggable LLM / embedding backend (Anthropic, OpenAI, Bedrock, vLLM, …)."""

    def complete(
        self,
        role: ModelRole,
        prompt: str,
        *,
        response_format: type[BaseModel] | None = None,
    ) -> str:
        """Return plain text or JSON text when structured output is requested."""
        ...
