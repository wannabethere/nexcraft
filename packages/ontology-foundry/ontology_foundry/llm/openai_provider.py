from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ontology_foundry.llm.defaults import (
    get_chat_api_key,
    get_chat_base_url,
    get_chat_model,
)
from ontology_foundry.llm.provider import ModelRole


class OpenAIChatProvider:
    """
    OpenAI-compatible chat adapter (DeepSeek V3 by default).
    Install: ontology-foundry[llm] and set DEEPSEEK_API_KEY (+ optional DEEPSEEK_BASE_URL).
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        client: Any | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._model = model or get_chat_model()
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as e:
                raise ImportError(
                    "OpenAIChatProvider requires the openai package. Install ontology-foundry[llm]"
                ) from e
            key = api_key or get_chat_api_key()
            if not key:
                raise RuntimeError(
                    "DEEPSEEK_API_KEY must be set for OpenAIChatProvider (DeepSeek default)."
                )
            self._client = OpenAI(
                api_key=key,
                base_url=base_url or get_chat_base_url(),
            )
        else:
            self._client = client

    def complete(
        self,
        role: ModelRole,
        prompt: str,
        *,
        response_format: type[BaseModel] | None = None,
    ) -> str:
        messages: list[dict[str, str]] = []
        if response_format is not None:
            messages.append(
                {
                    "role": "system",
                    "content": "Reply with a single JSON object only. No markdown fences.",
                }
            )
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }
        if response_format is not None:
            kwargs["response_format"] = {"type": "json_object"}

        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0].message
        content = getattr(choice, "content", None)
        return content if isinstance(content, str) else ""
