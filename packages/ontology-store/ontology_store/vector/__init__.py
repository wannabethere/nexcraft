"""Vector store layer — Qdrant.

Five concerns:
- `client.py`       — Qdrant client factory + connection lifecycle.
- `collections.py`  — `CollectionSpec` + the 14 collection definitions
                      (spine + per-tenant authoring + 4 event-sourced logs).
- `events.py`       — `EventEnvelope` + `EventKind` for the append-only logs.
- `embeddings.py`   — Embedder abstraction; OpenAI text-embedding-3-small default.
- `store.py`        — `QdrantDocumentStore`: legacy-compatible interface + raw points API.
- `hierarchy.py`    — `HierarchyVectorIndexer`: high-level upserts + `append_event`
                      for hier_t* / cards / sql_pairs / historical_qa / *_events.
"""
from ontology_store.vector.client import QdrantClientFactory, get_qdrant_client
from ontology_store.vector.collections import (
    CARD_EVENTS,
    CARDS,
    CAUSAL_EVENTS,
    CollectionSpec,
    HIER_T0_ORGS,
    HIER_T1_SOURCES,
    HIER_T2_CATALOGS,
    HIER_T3_SCHEMAS,
    HIER_T4_ASSETS,
    HIER_T5_FIELDS,
    HIER_T6_CODES,
    HISTORICAL_QA,
    PROTECTION_EVENTS,
    RELATION_EVENTS,
    SQL_PAIRS,
    all_collection_specs,
    resolve_collection_name,
)
from ontology_store.vector.embeddings import Embedder, OpenAIEmbedder
from ontology_store.vector.events import (
    COMMON_PAYLOAD_KEYS,
    EventEnvelope,
    EventKind,
)
from ontology_store.vector.hierarchy import HierarchyVectorIndexer
from ontology_store.vector.store import QdrantCollectionAdapter, QdrantDocumentStore

__all__ = [
    "QdrantClientFactory",
    "get_qdrant_client",
    "CollectionSpec",
    "HIER_T0_ORGS",
    "HIER_T1_SOURCES",
    "HIER_T2_CATALOGS",
    "HIER_T3_SCHEMAS",
    "HIER_T4_ASSETS",
    "HIER_T5_FIELDS",
    "HIER_T6_CODES",
    "CARDS",
    "SQL_PAIRS",
    "HISTORICAL_QA",
    "CAUSAL_EVENTS",
    "RELATION_EVENTS",
    "PROTECTION_EVENTS",
    "CARD_EVENTS",
    "all_collection_specs",
    "resolve_collection_name",
    "Embedder",
    "OpenAIEmbedder",
    "QdrantDocumentStore",
    "QdrantCollectionAdapter",
    "HierarchyVectorIndexer",
    "EventEnvelope",
    "EventKind",
    "COMMON_PAYLOAD_KEYS",
]
