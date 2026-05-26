"""Activity input models for GenieML post-SQL summary and chart generation."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class GeniemlNarrateParams(BaseModel):
    question: str
    sql: str
    conversation_id: str = "default"
    org_id: str = "default"
    row_count: int | None = None
    result_preview: dict[str, Any] = Field(default_factory=dict)


class GeniemlChartParams(BaseModel):
    question: str
    sql: str
    conversation_id: str = "default"
    org_id: str = "default"
    language: str = "English"
    sample_data: list[dict[str, Any]] = Field(default_factory=list)
    sample_column_values: dict[str, Any] = Field(default_factory=dict)
