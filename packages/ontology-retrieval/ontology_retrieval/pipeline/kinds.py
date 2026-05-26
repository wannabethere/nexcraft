"""Retrieval kinds — declarative definitions for each query type.

Each kind:
  - Has a unique id (and optional aliases for legacy callers).
  - Validates input against a Pydantic schema.
  - Declares which named sources it needs.
  - Provides a fetcher callable.

Implemented (v1):
  - asset_search             — search assets via Postgres ILIKE + concept overlap.
  - asset_by_rk              — hydrate a single asset by rk.
  - asset_list               — filtered enumeration.
  - lineage_upstream         — trace upstream lineage.
  - lineage_downstream       — trace downstream lineage.
  - lineage_trace            — both directions.

Stubs (registered with status='stub', return empty + diagnostic):
  - sql_pairs_search
  - instructions_search
  - historical_questions
  - card_search
  - metrics_search           (alias of asset_search w/ asset_kind=metric — works
                              once metric_metadata table is populated)
  - claims_by_asset

Legacy aliases (for compatibility with the previous RetrievalPipeline):
  - historical_questions ⇄ historical_qa_search
  - database_schemas     ⇄ asset_search
  - views                ⇄ asset_search (with asset_kind filter applied)
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ontology_store.schemas import (
    AssetHit,
    RetrievalScope,
    TableContext,
)

from ontology_retrieval.pipeline.base import (
    RetrievalContext,
    RetrievalKind,
    RetrievalResult,
    register_kind,
)
from ontology_retrieval.pipeline.sources import (
    PostgresAssetSource,
    PostgresLineageSource,
    QdrantSource,
)

# ───────────────────────────────────────────────────────────────────────────
# Input schemas
# ───────────────────────────────────────────────────────────────────────────

class AssetSearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = ""
    scope: RetrievalScope
    k: int = Field(default=10, ge=1, le=100)


class AssetByRkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rk: str = Field(min_length=3)


class AssetListIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: RetrievalScope
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class LineageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_rk: str = Field(min_length=3)
    direction: Literal["upstream", "downstream", "both"] = "both"
    edge_kinds: list[str] | None = None
    max_hops: int = Field(default=1, ge=1, le=5)


class TextQueryWithScopeIn(BaseModel):
    """Shared input for query-anchored kinds (sql_pairs, instructions, ...)."""
    model_config = ConfigDict(extra="forbid")
    query: str
    scope: RetrievalScope
    k: int = Field(default=10, ge=1, le=100)


class AssetRkScopedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_rk: str
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


# ───────────────────────────────────────────────────────────────────────────
# Fetchers — asset side
# ───────────────────────────────────────────────────────────────────────────

def _fetch_asset_search(ctx: RetrievalContext) -> RetrievalResult:
    src: PostgresAssetSource = ctx.source("postgres_assets")  # type: ignore[assignment]
    inp: AssetSearchIn = ctx.input  # type: ignore[assignment]
    hits = src.search_assets(query=inp.query, scope=inp.scope, k=inp.k)
    return RetrievalResult(
        kind=ctx.kind,
        data=[h.model_dump(mode="json") for h in hits],
        formatted_output={"documents": [h.model_dump(mode="json") for h in hits]},
        metadata={"count": len(hits), "query": inp.query},
    )


def _fetch_asset_by_rk(ctx: RetrievalContext) -> RetrievalResult:
    src: PostgresAssetSource = ctx.source("postgres_assets")  # type: ignore[assignment]
    inp: AssetByRkIn = ctx.input  # type: ignore[assignment]
    asset = src.get_asset(inp.rk)
    if asset is None:
        return RetrievalResult(
            kind=ctx.kind,
            data=None,
            formatted_output={"documents": []},
            metadata={"found": False, "rk": inp.rk},
        )
    return RetrievalResult(
        kind=ctx.kind,
        data=asset.model_dump(mode="json"),
        formatted_output={"documents": [asset.model_dump(mode="json")]},
        metadata={"found": True, "rk": inp.rk},
    )


def _fetch_asset_list(ctx: RetrievalContext) -> RetrievalResult:
    src: PostgresAssetSource = ctx.source("postgres_assets")  # type: ignore[assignment]
    inp: AssetListIn = ctx.input  # type: ignore[assignment]
    # Over-fetch by 1 to compute has_more
    items = src.list_assets(scope=inp.scope, limit=inp.limit + 1, offset=inp.offset)
    has_more = len(items) > inp.limit
    items = items[: inp.limit]
    return RetrievalResult(
        kind=ctx.kind,
        data=[h.model_dump(mode="json") for h in items],
        formatted_output={"documents": [h.model_dump(mode="json") for h in items]},
        metadata={
            "count": len(items),
            "limit": inp.limit,
            "offset": inp.offset,
            "has_more": has_more,
        },
    )


# ───────────────────────────────────────────────────────────────────────────
# Fetcher — vector-backed asset search (Qdrant) with Postgres hydration
# ───────────────────────────────────────────────────────────────────────────

def _fetch_asset_vector_search(ctx: RetrievalContext) -> RetrievalResult:
    """Vector-backed semantic search over hier_t4_assets, hydrated against
    Postgres for full TableContext payloads.

    Flow:
      1. Translate `scope` into payload filters (concepts, key_areas, source_id, asset_kind).
      2. Qdrant `search(query_text, payload_filter, k * 2)` over assets collection.
      3. For each hit, hydrate via PostgresAssetSource.get_asset(rk).
      4. Re-rank: vector score + concept-overlap bonus.
      5. Top-k returned.

    Falls back to pure Postgres `asset_search` when the qdrant_assets source is
    disabled (no client / no embedder).
    """
    qdrant_src: QdrantSource = ctx.source("qdrant_assets")  # type: ignore[assignment]
    pg_src: PostgresAssetSource = ctx.source("postgres_assets")  # type: ignore[assignment]
    inp: AssetSearchIn = ctx.input  # type: ignore[assignment]

    # Build payload filter from scope
    where: dict = {}
    if inp.scope.concepts:
        where["concepts"] = list(inp.scope.concepts)
    if inp.scope.key_areas:
        where["key_areas"] = list(inp.scope.key_areas)
    if inp.scope.causal_relations:
        where["causal_relations"] = list(inp.scope.causal_relations)
    if inp.scope.asset_kinds:
        where["asset_kind"] = {"$in": list(inp.scope.asset_kinds)}
    if inp.scope.source_ids:
        where["source_id"] = {"$in": list(inp.scope.source_ids)}
    if inp.scope.lifecycle_stages:
        where["lifecycle_stage"] = {"$in": list(inp.scope.lifecycle_stages)}

    vector_hits = qdrant_src.search(
        query_text=inp.query,
        payload_filter=where or None,
        k=max(inp.k * 2, 10),
    )

    # Fallback: when vector source returned nothing (disabled / empty collection),
    # fall through to the pure-Postgres asset_search kind for safety.
    if not vector_hits:
        ctx.diagnostics["fallback_to_postgres"] = True
        pg_hits = pg_src.search_assets(query=inp.query, scope=inp.scope, k=inp.k)
        items = [h.model_dump(mode="json") for h in pg_hits]
        return RetrievalResult(
            kind=ctx.kind,
            data=items,
            formatted_output={"documents": items},
            metadata={"count": len(items), "via": "postgres_fallback", "query": inp.query},
        )

    # Hydrate top hits via Postgres
    hydrated: list[dict] = []
    seen_rks: set[str] = set()
    for h in vector_hits:
        rk = h.get("id")
        if not rk or rk in seen_rks:
            continue
        seen_rks.add(rk)
        ctx_view = pg_src.get_asset(rk)
        if ctx_view is None:
            # Drop hits without a Postgres mirror; spine + qdrant should agree, but
            # during indexing/dropouts this happens transiently.
            continue
        record = ctx_view.model_dump(mode="json")
        record["score"] = float(h.get("score", 0.0))
        hydrated.append(record)
        if len(hydrated) >= inp.k:
            break

    return RetrievalResult(
        kind=ctx.kind,
        data=hydrated,
        formatted_output={"documents": hydrated},
        metadata={"count": len(hydrated), "via": "qdrant+postgres", "query": inp.query},
    )


# ───────────────────────────────────────────────────────────────────────────
# Fetchers — lineage side
# ───────────────────────────────────────────────────────────────────────────

def _fetch_lineage(ctx: RetrievalContext) -> RetrievalResult:
    src: PostgresLineageSource = ctx.source("postgres_lineage")  # type: ignore[assignment]
    inp: LineageIn = ctx.input  # type: ignore[assignment]
    nodes, edges = src.trace(
        asset_rk=inp.asset_rk,
        direction=inp.direction,
        edge_kinds=inp.edge_kinds,
        max_hops=inp.max_hops,
    )
    nodes_payload = [n.__dict__ for n in nodes]
    edges_payload = [e.__dict__ for e in edges]
    return RetrievalResult(
        kind=ctx.kind,
        data={"nodes": nodes_payload, "edges": edges_payload},
        formatted_output={"documents": edges_payload},
        metadata={
            "root_rk": inp.asset_rk,
            "direction": inp.direction,
            "max_hops": inp.max_hops,
            "node_count": len(nodes_payload),
            "edge_count": len(edges_payload),
        },
    )


# ───────────────────────────────────────────────────────────────────────────
# Stub fetcher factory — for kinds without backing yet
# ───────────────────────────────────────────────────────────────────────────

def _stub_fetcher(roadmap_note: str):
    def _f(ctx: RetrievalContext) -> RetrievalResult:
        return RetrievalResult(
            kind=ctx.kind,
            data=[],
            formatted_output={"documents": []},
            metadata={
                "stub": True,
                "roadmap": roadmap_note,
                "input": ctx.input.model_dump(mode="json"),
            },
        )
    return _f


# ───────────────────────────────────────────────────────────────────────────
# Registrations
# ───────────────────────────────────────────────────────────────────────────

# Active kinds — fully implemented in v1

register_kind(RetrievalKind(
    id="asset_search",
    description="Search assets (tables, views, materialized views) by free-text query + concept/key_area scope. Pure Postgres (ILIKE + concept overlap).",
    input_schema=AssetSearchIn,
    sources_required=("postgres_assets",),
    fetcher=_fetch_asset_search,
    cache_ttl_seconds=600,
    aliases=("database_schemas", "views"),
    status="active",
))

register_kind(RetrievalKind(
    id="asset_vector_search",
    description="Vector-backed semantic search over hier_t4_assets (Qdrant), hydrated via Postgres. Falls back to asset_search semantics when Qdrant is unavailable.",
    input_schema=AssetSearchIn,
    sources_required=("qdrant_assets", "postgres_assets"),
    fetcher=_fetch_asset_vector_search,
    cache_ttl_seconds=300,
    aliases=(),
    status="active",
))

register_kind(RetrievalKind(
    id="asset_by_rk",
    description="Hydrate one asset by its rk. Returns full TableContext with columns + descriptions.",
    input_schema=AssetByRkIn,
    sources_required=("postgres_assets",),
    fetcher=_fetch_asset_by_rk,
    cache_ttl_seconds=300,
    aliases=(),
    status="active",
))

register_kind(RetrievalKind(
    id="asset_list",
    description="Filtered enumeration of assets. Cursor-friendly via limit/offset.",
    input_schema=AssetListIn,
    sources_required=("postgres_assets",),
    fetcher=_fetch_asset_list,
    cache_ttl_seconds=120,
    aliases=(),
    status="active",
))

register_kind(RetrievalKind(
    id="lineage_trace",
    description="Walk lineage_edge in either or both directions up to max_hops.",
    input_schema=LineageIn,
    sources_required=("postgres_lineage",),
    fetcher=_fetch_lineage,
    cache_ttl_seconds=120,
    aliases=("lineage_upstream", "lineage_downstream"),
    status="active",
))


# Stub kinds — registered so the API surface is consistent; return empty + roadmap note

register_kind(RetrievalKind(
    id="sql_pairs_search",
    description="Search (question, sql) pairs by semantic similarity.",
    input_schema=TextQueryWithScopeIn,
    sources_required=(),  # no source needed for the stub; real impl needs postgres_sql_pair + qdrant_sql_pairs
    fetcher=_stub_fetcher("sql_pair table + qdrant collection pending; see retrieval_v2_spec §5.3"),
    cache_ttl_seconds=300,
    aliases=("sql_pairs",),
    status="stub",
))

register_kind(RetrievalKind(
    id="instructions_search",
    description="Retrieve instructions cards + legacy InstructionService entries.",
    input_schema=TextQueryWithScopeIn,
    sources_required=(),
    fetcher=_stub_fetcher("card kind=instruction + InstructionService bridge pending"),
    cache_ttl_seconds=600,
    aliases=("instructions",),
    status="stub",
))

register_kind(RetrievalKind(
    id="historical_qa_search",
    description="Search past Q&A pairs anchored on prior MCP ask calls.",
    input_schema=TextQueryWithScopeIn,
    sources_required=(),
    fetcher=_stub_fetcher("historical_qa table + qdrant collection pending"),
    cache_ttl_seconds=300,
    aliases=("historical_questions",),
    status="stub",
))

register_kind(RetrievalKind(
    id="cards_search",
    description="Semantic search over semantic-layer cards.",
    input_schema=TextQueryWithScopeIn,
    sources_required=(),
    fetcher=_stub_fetcher("card storage tables + qdrant cards collection pending"),
    cache_ttl_seconds=300,
    aliases=(),
    status="stub",
))

register_kind(RetrievalKind(
    id="metrics_search",
    description="Search metric assets by query + scope. Equivalent to asset_search with asset_kind=metric.",
    input_schema=TextQueryWithScopeIn,
    sources_required=(),
    fetcher=_stub_fetcher("metric_metadata + metric_dimension tables pending"),
    cache_ttl_seconds=600,
    aliases=("metrics",),
    status="stub",
))

register_kind(RetrievalKind(
    id="claims_by_asset",
    description="Return causal claims and candidates that reference an asset.",
    input_schema=AssetRkScopedIn,
    sources_required=(),
    fetcher=_stub_fetcher("claim + causal_candidate tables pending"),
    cache_ttl_seconds=120,
    aliases=(),
    status="stub",
))
