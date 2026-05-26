"""Data access objects — the *typed* read/write surface callers use.

- `HierarchyDAO` — writes the spine + extensions from an `MDLDocument`.
- `AnnotationDAO` — writes annotations with no-clobber + provenance audit.
- `InferenceDAO` — writes LLM-inferred side-outputs (inferred FKs → lineage_edge,
                   causal candidates, data-protection hints).
- `CardDAO` — writes / reads Postgres-backed semantic-layer cards.
- `AssetReader` — read paths used by the retrieval service.
"""
from ontology_store.dao.annotations import AnnotationDAO
from ontology_store.dao.cards import CardDAO, CardSummary, compute_content_hash
from ontology_store.dao.hierarchy import HierarchyDAO
from ontology_store.dao.inferences import InferenceDAO
from ontology_store.dao.reader import AssetReader
from ontology_store.dao.relations import RelationTypeDAO, RelationTypeIn
from ontology_store.dao.stats import (
    ColumnAggregate,
    ColumnStatDAO,
    TableSampleFacts,
)

__all__ = [
    "HierarchyDAO",
    "AnnotationDAO",
    "InferenceDAO",
    "CardDAO",
    "CardSummary",
    "compute_content_hash",
    "ColumnStatDAO",
    "ColumnAggregate",
    "TableSampleFacts",
    "RelationTypeDAO",
    "RelationTypeIn",
    "AssetReader",
]
