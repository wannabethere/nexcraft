"""FastAPI application factory.

Composes the asset router (and future routers — concept, lineage, etc.) into a
single app. Dependency injection keys off `Database` from ontology-store.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from ontology_store import Database

from ontology_retrieval.deps import set_database, set_pipeline
from ontology_retrieval.pipeline import (
    PipelineConfig,
    build_pipeline_from_config,
    default_config,
)
from ontology_retrieval.routes.assets import router as assets_router
from ontology_retrieval.routes.health import router as health_router
from ontology_retrieval.routes.lineage import router as lineage_router

logger = logging.getLogger("ontology_retrieval")


def create_app(
    *,
    db_url: str | None = None,
    pipeline_config: PipelineConfig | None = None,
) -> FastAPI:
    """Build a FastAPI app.

    Args:
        db_url: Falls back to ONTOLOGY_STORE_URL env var.
        pipeline_config: Optional PipelineConfig. If None, uses default_config()
            which wires the two Postgres-backed sources (assets + lineage).
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        url = db_url or os.environ.get("ONTOLOGY_STORE_URL")
        if not url:
            raise RuntimeError(
                "ONTOLOGY_STORE_URL must be set (or pass db_url to create_app)"
            )
        db = Database.from_url(url)
        set_database(db)
        cfg = pipeline_config or default_config()
        pipeline = build_pipeline_from_config(cfg, database=db)
        set_pipeline(pipeline)
        logger.info("ontology-retrieval connected to store at %s", _redact(url))
        logger.info(
            "retrieval pipeline initialized: %d kinds, sources=%s",
            len(pipeline.available_kinds), pipeline.configured_sources,
        )
        try:
            yield
        finally:
            db.engine.dispose()
            logger.info("ontology-retrieval store connection disposed")

    app = FastAPI(
        title="ontology-retrieval",
        version="0.1.0",
        description="Read API over the ontology hierarchy store.",
        lifespan=lifespan,
    )

    app.include_router(health_router)
    app.include_router(assets_router)
    app.include_router(lineage_router)
    return app


def _redact(url: str) -> str:
    """Best-effort masking of password in a SQLAlchemy URL for log lines."""
    import re
    return re.sub(r":[^:@/]+@", ":***@", url)
