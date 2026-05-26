"""ontology-retrieval pipeline — modular registry-driven retrieval.

Single entry point shape preserved from the legacy `RetrievalPipeline`:

    result = await pipeline.run(kind="asset_search", query="...", scope={...}, k=10)

Internally, instead of an if/elif dispatch, each kind is a declarative
`RetrievalKind` registered in a module-level registry. Kinds declare:
  - their input schema (Pydantic) — validated before execution
  - the named sources they require — resolved at pipeline construction
  - a `fetcher` callable that turns (input, sources) into a `RetrievalResult`

Adding a new kind is a new file/registration. Adding a new source kind
(Qdrant, filesystem, external API) is a new class. The pipeline core does
not change.
"""
from ontology_retrieval.pipeline.base import (
    KindStatus,
    RetrievalContext,
    RetrievalKind,
    RetrievalPipeline,
    RetrievalResult,
    register_kind,
    registry,
)
from ontology_retrieval.pipeline.cache import LRUCache, NullCache
from ontology_retrieval.pipeline.config import (
    KindConfig,
    PipelineConfig,
    SourceConfig,
    default_config,
)
from ontology_retrieval.pipeline.factory import build_pipeline_from_config
from ontology_retrieval.pipeline.sources import (
    PostgresAssetSource,
    PostgresLineageSource,
    QdrantSource,
    Source,
)

# Importing kinds registers them on the module registry as a side-effect.
from ontology_retrieval.pipeline import kinds  # noqa: F401

__all__ = [
    "RetrievalPipeline",
    "RetrievalKind",
    "RetrievalContext",
    "RetrievalResult",
    "KindStatus",
    "register_kind",
    "registry",
    "PipelineConfig",
    "KindConfig",
    "SourceConfig",
    "default_config",
    "build_pipeline_from_config",
    "LRUCache",
    "NullCache",
    "Source",
    "PostgresAssetSource",
    "PostgresLineageSource",
    "QdrantSource",
]
