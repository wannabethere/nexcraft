"""Pydantic shapes for the OntologyIngestionWorkflow.

These models are the contract between the YAML job spec (parsed by
`nexcraft_jobs.yaml_jobs.loader.load_job_file`) and the workflow's
`run(input: OntologyIngestionInput)` entry point.

Why a separate input model and not `PipelineConfig` directly:

  - `PipelineConfig.source.connection.password` is a plain string.
    Passing it through a Temporal workflow's history is a known anti-pattern;
    even though we're local-only in v1, an input model is the right shape
    to evolve toward Temporal's payload codec (env-var refs, KMS lookups).
  - The workflow may eventually take partial / phased configs (e.g. "only
    run column stats", "only run induction over an existing run's edges").
    A focused workflow input keeps that future open without bending
    `PipelineConfig`.

All models are JSON-round-trippable so Temporal can serialise them through
its default data converter.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ontology_pipeline.config import (
    LLMConfig,
    OutputConfig,
    PipelineBehavior,
    PipelineConfig,
    PostgresConnection,
    SemanticLayerConfig,
    SourceConfig,
    TableFilter,
)


# ───────────────────────────────────────────────────────────────────────────
# Workflow input
# ───────────────────────────────────────────────────────────────────────────


class OntologyIngestionInput(BaseModel):
    """Top-level input for the ingestion workflow.

    Mirrors `PipelineConfig` field-for-field so a YAML job spec under
    `input:` can be loaded by either entry point. The workflow uses
    `to_pipeline_config()` to materialise the existing `PipelineConfig`
    used by the rest of the pipeline.
    """
    model_config = ConfigDict(extra="forbid")

    source: SourceConfig
    tables: TableFilter = Field(default_factory=TableFilter)
    semantic_layer: SemanticLayerConfig = Field(default_factory=SemanticLayerConfig)
    output: OutputConfig
    llm: LLMConfig = Field(default_factory=LLMConfig)
    pipeline: PipelineBehavior = Field(default_factory=PipelineBehavior)

    # Workflow-only knobs (not on PipelineConfig).
    per_table_concurrency: int = Field(
        default=4, ge=1, le=64,
        description=(
            "Max in-flight per-table activities. Temporal will schedule "
            "up to this many `process_one_table_activity` calls in parallel "
            "via `asyncio.gather`. Bigger == faster but more LLM / DB pressure."
        ),
    )

    def to_pipeline_config(self) -> PipelineConfig:
        """Build the conventional `PipelineConfig` used by the rest of the pipeline."""
        return PipelineConfig(
            source=self.source,
            tables=self.tables,
            semantic_layer=self.semantic_layer,
            output=self.output,
            llm=self.llm,
            pipeline=self.pipeline,
        )


# ───────────────────────────────────────────────────────────────────────────
# Per-activity result shapes
# ───────────────────────────────────────────────────────────────────────────


class TableSpec(BaseModel):
    """Lightweight per-table descriptor returned by `introspect_source_activity`.

    Holds enough for `process_one_table_activity` to re-introspect a single
    table without re-running the whole source-introspection (which is
    expensive for sources with hundreds of tables).
    """
    schema_name: str
    name: str
    qualified_name: str
    asset_rk: str
    catalog: str | None = None


class PerTableResult(BaseModel):
    """Returned by `process_one_table_activity`. JSON-serialisable.

    The workflow accumulates `inferred_relationships` + `primary_concept`
    across tables to feed the post-pass activities (relation induction,
    cross-asset causal). The pipeline's existing `_process_table` populates
    everything needed.
    """
    qualified_name: str
    asset_rk: str
    outcome: Literal["created", "updated", "unchanged", "error"]
    native_columns_preserved: int = 0
    llm_calls: int = 0
    wall_time_s: float = 0.0
    error: str | None = None
    # Cross-table accumulators
    inferred_relationships: list[dict[str, Any]] = Field(default_factory=list)
    primary_concept: str | None = None


class PostPassResult(BaseModel):
    """Returned by each post-pass activity. JSON-serialisable."""
    stage: Literal[
        "cross_asset_causal",
        "induce_relation_schema",
        "validate_causal_candidates",
    ]
    llm_calls: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


class WorkflowSummary(BaseModel):
    """Workflow's terminal return value. Mirrors `PipelineRunResult` shape."""
    source_id: str
    tables_seen: int
    tables_processed: int
    tables_skipped_unchanged: int
    tables_errored: int
    total_llm_calls: int
    post_passes: list[PostPassResult] = Field(default_factory=list)
    per_table: list[PerTableResult] = Field(default_factory=list)
