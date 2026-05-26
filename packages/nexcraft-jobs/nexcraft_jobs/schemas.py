"""Shared execution schemas — imported by genieml-skills validators (Phase J.1)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ColumnSpec(BaseModel):
    name: str
    type: str = "unknown"


class GeniemlPostOutputConfig(BaseModel):
    """Optional summary + chart generation after SQL/data steps in Temporal."""

    question: str = ""
    sql: str = ""
    summarize: bool = True
    chart: bool = True
    language: str = "English"
    conversation_id: str = "default"
    org_id: str = "default"


class ExecutionResult(BaseModel):
    """Structured result from a FedSQL / nexcraft job activity."""

    rows: list[dict[str, Any]] = Field(default_factory=list)
    columns: list[ColumnSpec] = Field(default_factory=list)
    row_count: int = 0
    exec_time_ms: int = 0
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    success: bool = True
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Post-output artifacts: summary (narrate), chart (Vega-Lite).",
    )


class SourceBinding(BaseModel):
    """Resolves to a FedSQL source_id configured on the worker."""

    source_id: str
    dialect: Literal[
        "postgres",
        "snowflake",
        "bigquery",
        "iceberg",
        "delta",
        "sqlserver",
    ] = "postgres"


class FedSQLQueryInput(BaseModel):
    sql: str
    source_binding: SourceBinding | None = None
    source_id: str | None = Field(
        default=None,
        description="Shortcut when source_binding omitted.",
    )
    dialect: Literal[
        "postgres",
        "snowflake",
        "bigquery",
        "iceberg",
        "delta",
        "sqlserver",
    ] = "postgres"
    tenant_id: str = "default"
    query_id: str | None = None
    trace_id: str | None = None
    row_limit: int = Field(default=100_000, ge=1, le=10_000_000)
    timeout_seconds: int = Field(default=300, ge=5, le=3600)
    post_output: GeniemlPostOutputConfig | None = None

    def resolved_source_id(self) -> str:
        if self.source_binding is not None:
            return self.source_binding.source_id
        if self.source_id:
            return self.source_id
        raise ValueError("FedSQLQueryInput requires source_binding or source_id")
