"""Health endpoints — service liveness + DB connectivity check."""
from __future__ import annotations

import logging

from fastapi import APIRouter
from sqlalchemy import text

from ontology_retrieval.deps import get_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/db")
def health_db() -> dict[str, str | bool]:
    """Verify the DB connection by running a trivial SELECT."""
    try:
        with get_session() as s:
            s.execute(text("SELECT 1"))
        return {"status": "ok", "db_reachable": True}
    except Exception as exc:
        logger.error("DB health check failed: %s", exc)
        return {"status": "degraded", "db_reachable": False, "error": str(exc)}
