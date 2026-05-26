"""ontology-pipeline — auto-build pipeline for MDL + bottoms-up annotations.

Depends on ontology-foundry for LLM provider abstraction and shared models.

Entry points:
- `ontology_pipeline.pipeline.run` — programmatic entry
- `ontology_pipeline.cli.main` — CLI entry (`ontology-pipeline run --config <path>`)
"""
from ontology_pipeline.config import PipelineConfig, SourceConfig, TableFilter
from ontology_pipeline.enrich import (
    CAUSAL_PREDICATES,
    CausalDependencyEnricher,
    ColumnSemanticsEnricher,
    DataProtectionEnricher,
    EnrichmentResult,
    EnrichmentStage,
    InferredRelationship,
    RelationshipInferenceEnricher,
    RichDescriptionEnricher,
)
from ontology_pipeline.models import (
    AssetAnnotations,
    ColumnInfo,
    GeneratedMDL,
    IntrospectionResult,
    PipelineRunResult,
    TableInfo,
)
from ontology_pipeline.pipeline import run

__all__ = [
    "PipelineConfig",
    "SourceConfig",
    "TableFilter",
    "IntrospectionResult",
    "TableInfo",
    "ColumnInfo",
    "GeneratedMDL",
    "AssetAnnotations",
    "PipelineRunResult",
    "run",
    # Enrichment stages
    "EnrichmentStage",
    "EnrichmentResult",
    "RichDescriptionEnricher",
    "ColumnSemanticsEnricher",
    "DataProtectionEnricher",
    "RelationshipInferenceEnricher",
    "InferredRelationship",
    "CausalDependencyEnricher",
    "CAUSAL_PREDICATES",
]
