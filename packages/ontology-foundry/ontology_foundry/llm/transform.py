from __future__ import annotations

import json
from typing import TypeVar

from pydantic import BaseModel

from ontology_foundry.llm.provider import ModelProvider, ModelRole

T = TypeVar("T", bound=BaseModel)


def llm_structured_transform(
    provider: ModelProvider,
    role: ModelRole,
    prompt: str,
    response_model: type[T],
) -> T:
    """
    LLM transformation with Pydantic validation (extraction §3.7 — structured output).
    Expects the provider to return a JSON string when `response_format` is set.
    """
    raw = provider.complete(role, prompt, response_format=response_model)
    if not isinstance(raw, str):
        raise TypeError(f"Provider must return str, got {type(raw)}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM response was not valid JSON: {raw[:200]}…") from e
    return response_model.model_validate(data)


def llm_text_transform(provider: ModelProvider, role: ModelRole, prompt: str) -> str:
    """Free-text completion for summaries, prose card drafts, etc."""
    return provider.complete(role, prompt, response_format=None)
