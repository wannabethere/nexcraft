from __future__ import annotations

from pydantic import BaseModel

from ontology_foundry.llm.provider import ModelRole


class StaticJsonProvider:
    """Deterministic provider for tests — maps role → JSON string responses."""

    def __init__(self, responses: dict[ModelRole, str]) -> None:
        self._responses = responses

    def complete(
        self,
        role: ModelRole,
        prompt: str,
        *,
        response_format: type[BaseModel] | None = None,
    ) -> str:
        if role not in self._responses:
            raise KeyError(f"No stubbed response for role={role!s}")
        return self._responses[role]
