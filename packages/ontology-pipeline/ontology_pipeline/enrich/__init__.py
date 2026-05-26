"""Enrichment stages — pluggable LLM-driven enrichments applied to MDL after the
deterministic build but before annotation + storage.

Each stage implements `EnrichmentStage` (a Protocol) with an `apply(mdl, ctx)`
method. The orchestrator chains them in a configured order. Stages are
idempotent at the (asset, content_hash) level — re-running against the same
input produces the same output and the no-clobber rule preserves any prior
human edits.

Stages shipped in v1 (each gated by a config flag in `PipelineBehavior`):

  - `RichDescriptionEnricher`         → business_purpose, use_cases,
                                         update_frequency, key_relationships,
                                         performance_considerations
  - `ColumnSemanticsEnricher`         → semantic_unit, business_meaning,
                                         is_business_key
  - `DataProtectionEnricher`          → is_pii, pii_categories, sensitivity_class
                                         per column + asset-level RLS/CLS hints
  - `RelationshipInferenceEnricher`   → FK suggestions for tables that lack any
                                         declared FK (LLM proposes; lands as
                                         `lineage_edge` candidates)
  - `CausalDependencyEnricher`        → causal_node participation roles + causal
                                         candidate edges with column-level
                                         evidence; LLM-driven (not statistical;
                                         statistical causal discovery in
                                         `ontology_foundry.causal/` runs on data,
                                         not metadata, and lives downstream)

All stages reuse `ontology_foundry.llm` providers (OpenAIChatProvider by default)
+ structured Pydantic output via `llm_structured_transform`. Identical pattern
to the existing description fill; the new stages just have different prompts +
return shapes.
"""
from ontology_pipeline.enrich.asset_surface import (
    build_asset_lookup,
    build_column_lookup,
    render_asset_one_liner,
    render_asset_surface,
    render_column_brief,
    render_evidence_block,
)
from ontology_pipeline.enrich.base import (
    EnrichmentContext,
    EnrichmentResult,
    EnrichmentStage,
    EnrichmentStageRegistry,
)
from ontology_pipeline.enrich.causal import (
    CAUSAL_PREDICATES,
    CausalDependencyEnricher,
)
from ontology_pipeline.enrich.cross_asset_causal import (
    AssetCluster,
    ClusterContext,
    CrossAssetCausalEnricher,
)
from ontology_pipeline.enrich.data_protection import DataProtectionEnricher
from ontology_pipeline.enrich.description import RichDescriptionEnricher
from ontology_pipeline.enrich.relationships import (
    InferredRelationship,
    RelationshipInferenceEnricher,
)
from ontology_pipeline.enrich.semantics import ColumnSemanticsEnricher

__all__ = [
    "EnrichmentStage",
    "EnrichmentResult",
    "EnrichmentContext",
    "EnrichmentStageRegistry",
    "render_asset_surface",
    "render_asset_one_liner",
    "render_column_brief",
    "render_evidence_block",
    "build_asset_lookup",
    "build_column_lookup",
    "RichDescriptionEnricher",
    "ColumnSemanticsEnricher",
    "DataProtectionEnricher",
    "InferredRelationship",
    "RelationshipInferenceEnricher",
    "CausalDependencyEnricher",
    "CrossAssetCausalEnricher",
    "AssetCluster",
    "ClusterContext",
    "CAUSAL_PREDICATES",
]
