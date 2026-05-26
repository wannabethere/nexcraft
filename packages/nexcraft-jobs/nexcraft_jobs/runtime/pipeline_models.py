"""Input models for the generic dstools multi-step pipeline workflow.

A pipeline is an ordered list of steps. Each step is one of:
  - ``fedsql``         : run a SQL statement against a FedSQL source → rows.
  - ``dstools_sql``    : render a dstools sql_template tool, then run it via FedSQL.
  - ``dstools_python`` : run a dstools python tool, optionally consuming a prior
                         step's rows (Shapley, ARIMA, stats, …) → rows.

The final step's result is returned as the job's ``ExecutionResult``. This is
the target of the multi-step nexcraft YAML the SQL agent generates.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from nexcraft_jobs.schemas import ColumnSpec, ExecutionResult, GeniemlPostOutputConfig


class PipelineStep(BaseModel):
    id: str
    kind: Literal["fedsql", "dstools_sql", "dstools_python"]

    # fedsql + dstools_sql (warehouse execution)
    sql: str | None = None
    source_id: str | None = None
    dialect: Literal[
        "postgres", "snowflake", "bigquery", "iceberg", "delta", "sqlserver"
    ] = "postgres"

    # dstools steps
    tool: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    # dstools_python: feed a prior step's rows into this tool under `data_param`.
    consumes: str | None = None
    data_param: str = "data"

    @model_validator(mode="after")
    def _check_kind_fields(self) -> "PipelineStep":
        if self.kind == "fedsql" and not (self.sql and self.sql.strip()):
            raise ValueError(f"fedsql step {self.id!r} requires `sql`")
        if self.kind in ("dstools_sql", "dstools_python") and not (
            self.tool and self.tool.strip()
        ):
            raise ValueError(f"{self.kind} step {self.id!r} requires `tool`")
        return self


class DstoolsPipelineInput(BaseModel):
    steps: list[PipelineStep] = Field(min_length=1)
    final_step: str | None = Field(
        default=None,
        description="Step id whose result is the job answer. Defaults to the last step.",
    )
    tenant_id: str = "default"
    query_id: str | None = None
    trace_id: str | None = None
    row_limit: int = Field(default=100_000, ge=1, le=10_000_000)
    timeout_seconds: int = Field(default=600, ge=5, le=3600)
    post_output: GeniemlPostOutputConfig | None = None

    @model_validator(mode="after")
    def _check_refs(self) -> "DstoolsPipelineInput":
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("pipeline step ids must be unique")
        idset = set(ids)
        for s in self.steps:
            if s.consumes and s.consumes not in idset:
                raise ValueError(
                    f"step {s.id!r} consumes unknown step {s.consumes!r}"
                )
        if self.final_step and self.final_step not in idset:
            raise ValueError(f"final_step {self.final_step!r} not in steps")
        return self

    def resolved_final(self) -> str:
        return self.final_step or self.steps[-1].id


def tool_output_to_execution_result(out: Any) -> ExecutionResult:
    """Normalize a dstools tool result (TabularOutput/ScalarOutput/dict) into an
    ExecutionResult. Pure — unit-testable without Temporal."""
    if isinstance(out, ExecutionResult):
        return out
    data: Any = out
    if hasattr(out, "model_dump"):
        data = out.model_dump(mode="json")
    if isinstance(data, dict):
        rows = data.get("data")
        if isinstance(rows, list):
            schema = data.get("schema") or {}
            cols = [ColumnSpec(name=str(c), type=str(t)) for c, t in schema.items()]
            return ExecutionResult(rows=rows, columns=cols, row_count=len(rows))
        if "value" in data:
            return ExecutionResult(
                rows=[{"value": data["value"]}],
                columns=[ColumnSpec(name="value")],
                row_count=1,
            )
        if "raw" in data:
            raw = data["raw"]
            if isinstance(raw, list):
                return ExecutionResult(rows=raw, row_count=len(raw))
            return ExecutionResult(rows=[{"value": raw}], row_count=1)
    if isinstance(data, list):
        return ExecutionResult(rows=data, row_count=len(data))
    return ExecutionResult(rows=[{"value": data}], row_count=1)
