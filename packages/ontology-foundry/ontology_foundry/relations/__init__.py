"""Relation extraction: typed edges between linked entities, plus the tools to
induce a relation schema (TBox) from what was observed.

Composes with the existing foundry layers:
  * NER → linker → spans   (`ner.HybridNerPipeline`, `linking.SeedFirstEntityLinker`)
  * claims (optional)      (`models.ClaimArtifact`)
  * relation extraction    (`RelationPipeline`)
  * schema induction       (`induction.induce_schema`)

This package stops at structured artifacts (`RelationArtifact`, `RelationSchema`).
Rendering those into ontology cards, RDF/OWL, or any other storage format is an
external transformation handled outside the foundry.
"""

from ontology_foundry.relations.induction import (
    InducedPredicate,
    induce_schema,
    novel_promotion_candidates,
)
from ontology_foundry.relations.pipeline import RelationPipeline, dedupe_keep_best
from ontology_foundry.relations.schema import RelationSchema, RelationType
from ontology_foundry.relations.seeds import RelationSeed, SeedPack, default_pack_dirs
from ontology_foundry.relations.stages import (
    RelationStage,
    SeededLlmRelationStage,
    StubRelationStage,
)

__all__ = [
    "InducedPredicate",
    "RelationPipeline",
    "RelationSchema",
    "RelationSeed",
    "RelationStage",
    "RelationType",
    "SeedPack",
    "SeededLlmRelationStage",
    "StubRelationStage",
    "dedupe_keep_best",
    "default_pack_dirs",
    "induce_schema",
    "novel_promotion_candidates",
]
