"""Asset HTTP endpoints — thin transformations over internal pipeline kinds.

Each route:
  1. Parses the request body (purpose-built schema; no pipeline internals leak).
  2. Calls the internal RetrievalPipeline with the appropriate kind id.
  3. Shapes the response back to the route's documented contract.

This keeps the HTTP surface narrow and stable while the pipeline's internal
shape (sources, caching, kinds) evolves freely.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from ontology_store.schemas import AssetHit, RetrievalScope, TableContext

from ontology_retrieval.deps import get_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assets", tags=["assets"])


# ───────────────────────────────────────────────────────────────────────────
# Response shapes
# ───────────────────────────────────────────────────────────────────────────

class ListResponse(BaseModel):
    items: list[AssetHit]
    limit: int
    offset: int
    has_more: bool


class SearchRequest(BaseModel):
    query: str = Field(default="", description="Free-text query.")
    scope: RetrievalScope
    k: int = Field(default=10, ge=1, le=100)


class SearchResponse(BaseModel):
    items: list[AssetHit]
    k: int


class ListRequest(BaseModel):
    scope: RetrievalScope
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


# ───────────────────────────────────────────────────────────────────────────
# Routes — each delegates to a pipeline kind
# ───────────────────────────────────────────────────────────────────────────

@router.get("/by-rk", response_model=TableContext)
async def get_by_rk(
    rk: Annotated[str, Query(..., description="The asset rk", min_length=3)],
) -> TableContext:
    """Hydrate a single asset by its rk. 404 if not found."""
    pipeline = get_pipeline()
    try:
        result = await pipeline.run("asset_by_rk", rk=rk)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if result.metadata.get("found") is False or result.data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"asset_rk {rk!r} not found",
        )
    return TableContext.model_validate(result.data)


@router.post("/list", response_model=ListResponse)
async def list_assets(req: ListRequest) -> ListResponse:
    """Filtered enumeration of assets. Scope drives filtering."""
    pipeline = get_pipeline()
    try:
        result = await pipeline.run(
            "asset_list",
            scope=req.scope.model_dump(),
            limit=req.limit,
            offset=req.offset,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    items = [AssetHit.model_validate(d) for d in (result.data or [])]
    return ListResponse(
        items=items,
        limit=req.limit,
        offset=req.offset,
        has_more=bool(result.metadata.get("has_more", False)),
    )


@router.post("/search", response_model=SearchResponse)
async def search_assets(req: SearchRequest) -> SearchResponse:
    """Search assets by query + concept/key_area-aware ranking."""
    pipeline = get_pipeline()
    try:
        result = await pipeline.run(
            "asset_search",
            query=req.query,
            scope=req.scope.model_dump(),
            k=req.k,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    items = [AssetHit.model_validate(d) for d in (result.data or [])]
    return SearchResponse(items=items, k=req.k)
