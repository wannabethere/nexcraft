"""Enrichment stage Protocol + result envelope.

A stage takes the MDL-so-far + a context (LLM provider, vocab, existing cards)
and returns an `EnrichmentResult` describing what it changed. The orchestrator
applies them in registered order, accumulating LLM-call counts + per-stage
provenance for telemetry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ontology_foundry.llm.provider import ModelProvider

from ontology_pipeline.models import GeneratedMDL


@dataclass
class EnrichmentResult:
    """What a stage did during one invocation. Always returned; never raises."""
    stage_name: str
    fields_updated: list[str] = field(default_factory=list)
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    wall_time_ms: int = 0
    warnings: list[str] = field(default_factory=list)
    # Optional structured side-output (e.g. inferred relationships) the
    # orchestrator routes to a downstream writer / sink.
    side_output: dict[str, Any] = field(default_factory=dict)


@dataclass
class EnrichmentContext:
    """Per-asset context passed into each stage.

    Stages need to know who they're enriching (source/catalog/schema), the LLM
    provider to use, and any tenant-side vocabulary (cards, key_areas).

    `tabular_bundle` is an optional `ontology_foundry.context.TabularContextBundle`
    built by the profiling pre-pass (`ontology_pipeline.profile.TableProfiler`).
    Enrichers that ground in column stats (ColumnSemantics, DataProtection,
    Causal*, RelationshipInference) consult it via the
    `render_tabular_context` foundry helper. None when profiling was skipped
    or failed for this asset.
    """
    source_id: str
    catalog: str | None
    schema_name: str
    provider: ModelProvider | None
    llm_model_id: str | None = None
    tabular_bundle: Any | None = None


class EnrichmentStage(Protocol):
    """Pluggable enrichment stage."""

    name: str
    """Stable id used in config + telemetry. e.g. 'rich_description'."""

    def apply(self, mdl: GeneratedMDL, ctx: EnrichmentContext) -> EnrichmentResult: ...


class EnrichmentStageRegistry:
    """Optional registry — useful when stages are constructed by name from config.

    The orchestrator can build stages directly; the registry exists for
    operator-facing flexibility (enable/disable stages via YAML).
    """

    def __init__(self) -> None:
        self._stages: dict[str, EnrichmentStage] = {}

    def register(self, stage: EnrichmentStage) -> None:
        if stage.name in self._stages:
            raise ValueError(f"Enrichment stage {stage.name!r} already registered")
        self._stages[stage.name] = stage

    def get(self, name: str) -> EnrichmentStage:
        try:
            return self._stages[name]
        except KeyError as exc:
            raise KeyError(f"Unknown enrichment stage: {name!r}") from exc

    def all(self) -> list[EnrichmentStage]:
        return list(self._stages.values())
