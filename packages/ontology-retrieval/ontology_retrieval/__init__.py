"""ontology-retrieval — Python retrieval pipeline + HTTP search APIs over ontology-store.

Two distinct usage modes:

  1. **Internal Python import** (primary). Used to replace the genieml-agents
     retrieval flow and the compliance-skill retrieval flow once the ontology
     store is populated. See `ontology_retrieval.pipeline` for the new shape
     and `ontology_retrieval.compat` for stand-in adapters.

  2. **HTTP API**. Search-focused endpoints (`/assets/*`, `/lineage/trace`,
     `/health/*`) that internally call the pipeline. The pipeline itself is
     NOT exposed as a generic HTTP endpoint.

Entry points:
- `ontology_retrieval.app:create_app()`             — FastAPI app factory.
- `ontology_retrieval.cli:main`                     — `ontology-retrieval serve`.
- `ontology_retrieval.pipeline.RetrievalPipeline`   — internal pipeline class.
- `ontology_retrieval.compat.LegacyRetrievalPipeline` — drop-in replacement.
- `ontology_retrieval.compat.build_legacy_pipeline`   — one-shot constructor.
"""
from ontology_retrieval.app import create_app
from ontology_retrieval.compat import (
    LegacyRetrievalHelper,
    LegacyRetrievalPipeline,
    build_legacy_pipeline,
)

__all__ = [
    "create_app",
    "LegacyRetrievalPipeline",
    "LegacyRetrievalHelper",
    "build_legacy_pipeline",
]
