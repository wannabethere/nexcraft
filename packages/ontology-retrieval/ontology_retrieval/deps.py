"""FastAPI dependencies — database access + retrieval pipeline.

`Database` + `RetrievalPipeline` are set once at app startup via the
lifespan handler and accessed by request handlers through these getters.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from ontology_store import Database

from ontology_retrieval.pipeline import RetrievalPipeline

_db: Database | None = None
_pipeline: RetrievalPipeline | None = None


def set_database(db: Database) -> None:
    global _db
    _db = db


def get_database() -> Database:
    if _db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not initialized",
        )
    return _db


def set_pipeline(pipeline: RetrievalPipeline) -> None:
    global _pipeline
    _pipeline = pipeline


def get_pipeline() -> RetrievalPipeline:
    if _pipeline is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Retrieval pipeline not initialized",
        )
    return _pipeline


@contextmanager
def get_session() -> Iterator[Session]:
    """Context-manager session for use inside route handlers."""
    db = get_database()
    with db.session() as s:
        yield s
