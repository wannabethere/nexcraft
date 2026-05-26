"""Validated shape for a Temporal workflow job YAML file."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class TemporalJobSpec(BaseModel):
    """Single workflow invocation definition."""

    version: Literal[1] = 1
    name: str | None = Field(default=None, description="Friendly label for logs.")
    workflow_type: str = Field(
        ...,
        description="Temporal workflow type name (matches @workflow.defn name).",
    )
    task_queue: str
    temporal_target: str = Field(default="localhost:7233")
    workflow_id: str | None = Field(
        default=None,
        description="Fixed id; omit for uuid suffix (recommended for ad hoc runs).",
    )
    workflow_id_prefix: str | None = Field(
        default=None,
        description="If workflow_id unset, build id as {prefix}-{uuid}.",
    )
    wait_for_result: bool = Field(default=True)
    input: dict[str, Any] = Field(default_factory=dict)
