"""Lineage HTTP endpoints — thin wrapper over the lineage_trace pipeline kind."""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ontology_retrieval.deps import get_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/lineage", tags=["lineage"])


class LineageRequest(BaseModel):
    asset_rk: str = Field(min_length=3, description="The starting asset rk.")
    direction: Literal["upstream", "downstream", "both"] = "both"
    edge_kinds: list[str] | None = Field(
        default=None,
        description="Optional filter for lineage_edge.edge_kind (e.g., ['depends_on', 'derived_from']).",
    )
    max_hops: int = Field(default=1, ge=1, le=5)


class LineageNode(BaseModel):
    rk: str
    kind: str
    hop: int
    name: str | None = None
    schema_name: str | None = None


class LineageEdge(BaseModel):
    from_rk: str
    from_kind: str
    to_rk: str
    to_kind: str
    edge_kind: str
    evidence_kind: str
    confidence: float | None = None


class LineageResponse(BaseModel):
    root_rk: str
    direction: str
    max_hops: int
    nodes: list[LineageNode]
    edges: list[LineageEdge]


@router.post("/trace", response_model=LineageResponse)
async def trace_lineage(req: LineageRequest) -> LineageResponse:
    """Walk the lineage_edge graph from a root rk for N hops."""
    pipeline = get_pipeline()
    try:
        result = await pipeline.run(
            "lineage_trace",
            asset_rk=req.asset_rk,
            direction=req.direction,
            edge_kinds=req.edge_kinds,
            max_hops=req.max_hops,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    data = result.data or {"nodes": [], "edges": []}
    return LineageResponse(
        root_rk=req.asset_rk,
        direction=req.direction,
        max_hops=req.max_hops,
        nodes=[LineageNode.model_validate(n) for n in data.get("nodes", [])],
        edges=[LineageEdge.model_validate(e) for e in data.get("edges", [])],
    )
