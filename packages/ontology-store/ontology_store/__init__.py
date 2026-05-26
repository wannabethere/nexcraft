"""ontology-store — shared persistence layer for the foundry.

Three concerns, three submodules:
- `ontology_store.db`      — SQLAlchemy ORM models + engine/session management.
- `ontology_store.dao`     — typed DAOs that callers actually use (writers + readers).
- `ontology_store.schemas` — Pydantic schemas shared between pipeline and retrieval.

Importable surface:

    from ontology_store import (
        # Connection
        Database, get_session,
        # DAO
        HierarchyDAO, AnnotationDAO, AssetReader,
        # Pydantic schemas
        OrganizationIn, SourceIn, CatalogIn,
        MDLDocument, AssetAnnotations,
        TableContext, AssetHit, AssetSearchFilters, RetrievalScope,
    )
"""
from ontology_store.dao import (
    AnnotationDAO,
    AssetReader,
    CardDAO,
    CardSummary,
    HierarchyDAO,
    InferenceDAO,
)
from ontology_store.db.engine import Database, get_session
from ontology_store.schemas import (
    AssetAnnotations,
    AssetHit,
    AssetSearchFilters,
    CatalogIn,
    MDLColumn,
    MDLColumnProperties,
    MDLDocument,
    MDLMaterialization,
    MDLModel,
    MDLViewDefinition,
    OrganizationIn,
    RetrievalScope,
    SourceIn,
    TableContext,
    TableContextColumn,
)

__all__ = [
    "Database",
    "get_session",
    "HierarchyDAO",
    "AnnotationDAO",
    "InferenceDAO",
    "CardDAO",
    "CardSummary",
    "AssetReader",
    "OrganizationIn",
    "SourceIn",
    "CatalogIn",
    "MDLDocument",
    "MDLModel",
    "MDLColumn",
    "MDLColumnProperties",
    "MDLMaterialization",
    "MDLViewDefinition",
    "AssetAnnotations",
    "TableContext",
    "TableContextColumn",
    "AssetHit",
    "AssetSearchFilters",
    "RetrievalScope",
]

# Vector layer is optional — only available when the [vector] extra is installed.
# Importing it conditionally avoids forcing qdrant-client / langchain on every caller.
try:
    from ontology_store.vector import (  # noqa: F401
        CARDS,
        CollectionSpec,
        Embedder,
        HIER_T0_ORGS,
        HIER_T1_SOURCES,
        HIER_T2_CATALOGS,
        HIER_T3_SCHEMAS,
        HIER_T4_ASSETS,
        HIER_T5_FIELDS,
        HIER_T6_CODES,
        HISTORICAL_QA,
        HierarchyVectorIndexer,
        OpenAIEmbedder,
        QdrantClientFactory,
        QdrantCollectionAdapter,
        QdrantDocumentStore,
        SQL_PAIRS,
        all_collection_specs,
        get_qdrant_client,
        resolve_collection_name,
    )
    __all__.extend([
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
        "all_collection_specs",
        "resolve_collection_name",
        "Embedder",
        "OpenAIEmbedder",
        "QdrantClientFactory",
        "get_qdrant_client",
        "QdrantDocumentStore",
        "QdrantCollectionAdapter",
        "HierarchyVectorIndexer",
    ])
except ImportError:
    # Vector deps not installed; vector exports remain unavailable.
    pass
