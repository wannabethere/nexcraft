"""Sources — abstract backings the pipeline orchestrates.

Source is a Protocol. Concrete classes wrap an underlying store (ontology-store
DAOs, Qdrant client, filesystem dir, etc.) and expose narrow methods that the
kind fetchers call.

Adding a new source kind: subclass and add to the factory.
Adding a new source instance: list it in the PipelineConfig and the factory
will create it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterator, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ontology_store import AssetReader, Database
from ontology_store.db.models import LineageEdge, TableMetadata
from ontology_store.schemas import (
    AssetHit,
    RetrievalScope,
    TableContext,
)

logger = logging.getLogger(__name__)


class Source(Protocol):
    """Marker Protocol. Each subtype carries its own method surface; kinds
    cast to the concrete type at use site."""
    name: str
    kind: str


# ───────────────────────────────────────────────────────────────────────────
# Postgres asset source
# ───────────────────────────────────────────────────────────────────────────

class PostgresAssetSource:
    """Wraps `ontology_store.AssetReader`. Used by asset_search / asset_by_rk / asset_list kinds."""

    kind = "postgres_asset"

    def __init__(self, *, database: Database, name: str = "postgres_assets") -> None:
        self.name = name
        self._db = database

    def get_asset(self, asset_rk: str) -> TableContext | None:
        with self._db.session() as s:
            return AssetReader(s).get_asset(asset_rk)

    def list_assets(
        self, *, scope: RetrievalScope, limit: int = 50, offset: int = 0,
    ) -> list[AssetHit]:
        with self._db.session() as s:
            return AssetReader(s).list_assets(scope=scope, limit=limit, offset=offset)

    def search_assets(
        self, *, query: str, scope: RetrievalScope, k: int = 10,
    ) -> list[AssetHit]:
        with self._db.session() as s:
            return AssetReader(s).search_assets(query=query, scope=scope, k=k)


# ───────────────────────────────────────────────────────────────────────────
# Postgres lineage source
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class LineageNodeView:
    rk: str
    kind: str
    hop: int
    name: str | None = None
    schema_name: str | None = None


@dataclass
class LineageEdgeView:
    from_rk: str
    from_kind: str
    to_rk: str
    to_kind: str
    edge_kind: str
    evidence_kind: str
    confidence: float | None = None


class PostgresLineageSource:
    """Reads from `lineage_edge` for lineage trace queries."""

    kind = "postgres_lineage"

    def __init__(self, *, database: Database, name: str = "postgres_lineage") -> None:
        self.name = name
        self._db = database

    def trace(
        self,
        *,
        asset_rk: str,
        direction: str = "both",
        edge_kinds: list[str] | None = None,
        max_hops: int = 1,
    ) -> tuple[list[LineageNodeView], list[LineageEdgeView]]:
        """Walk the lineage_edge graph up to max_hops from asset_rk."""
        if max_hops < 1:
            return [], []

        nodes: dict[str, LineageNodeView] = {}
        edges: list[LineageEdgeView] = []

        # Seed
        with self._db.session() as s:
            seed_node = self._lookup_node(s, asset_rk, hop=0)
            nodes[asset_rk] = seed_node

            frontier_up = {asset_rk} if direction in ("upstream", "both") else set()
            frontier_dn = {asset_rk} if direction in ("downstream", "both") else set()

            for hop in range(1, max_hops + 1):
                next_up: set[str] = set()
                next_dn: set[str] = set()

                if frontier_up:
                    rows = self._fetch_edges(s, to_rks=frontier_up, edge_kinds=edge_kinds)
                    for r in rows:
                        edges.append(self._edge_to_view(r))
                        if r.from_rk not in nodes:
                            nodes[r.from_rk] = self._lookup_node(s, r.from_rk, hop=hop)
                            next_up.add(r.from_rk)
                if frontier_dn:
                    rows = self._fetch_edges(s, from_rks=frontier_dn, edge_kinds=edge_kinds)
                    for r in rows:
                        edges.append(self._edge_to_view(r))
                        if r.to_rk not in nodes:
                            nodes[r.to_rk] = self._lookup_node(s, r.to_rk, hop=hop)
                            next_dn.add(r.to_rk)

                frontier_up = next_up
                frontier_dn = next_dn

        return list(nodes.values()), edges

    # ── internals ───────────────────────────────────────────────────────

    @staticmethod
    def _lookup_node(session: Session, rk: str, *, hop: int) -> LineageNodeView:
        tbl = session.get(TableMetadata, rk)
        if tbl is not None:
            return LineageNodeView(
                rk=rk, kind=("view" if tbl.is_view else "table"),
                hop=hop, name=tbl.name,
            )
        # Unknown to spine — return a stub. Could be an external rk or a future API rk.
        return LineageNodeView(rk=rk, kind="unknown", hop=hop)

    @staticmethod
    def _fetch_edges(
        session: Session,
        *,
        from_rks: set[str] | None = None,
        to_rks: set[str] | None = None,
        edge_kinds: list[str] | None = None,
    ) -> Iterator[LineageEdge]:
        stmt = select(LineageEdge).where(LineageEdge.active.is_(True))
        if from_rks:
            stmt = stmt.where(LineageEdge.from_rk.in_(from_rks))
        if to_rks:
            stmt = stmt.where(LineageEdge.to_rk.in_(to_rks))
        if edge_kinds:
            stmt = stmt.where(LineageEdge.edge_kind.in_(edge_kinds))
        return session.execute(stmt).scalars().all()

    @staticmethod
    def _edge_to_view(row: LineageEdge) -> LineageEdgeView:
        return LineageEdgeView(
            from_rk=row.from_rk, from_kind=row.from_kind,
            to_rk=row.to_rk, to_kind=row.to_kind,
            edge_kind=row.edge_kind, evidence_kind=row.evidence_kind,
            confidence=row.confidence,
        )


# ───────────────────────────────────────────────────────────────────────────
# Qdrant source — backed by ontology_store.vector.QdrantDocumentStore
# ───────────────────────────────────────────────────────────────────────────

class QdrantSource:
    """Qdrant vector-search source.

    Wraps `ontology_store.vector.QdrantDocumentStore` over one collection.
    The fetcher calls `search(query_text, payload_filter, k)` to get ranked
    hits with full payloads.

    Construction:
      - Pass `store=QdrantDocumentStore(...)` directly, OR
      - Pass `collection`, `qdrant_client`, `embedder` and the source builds
        the store internally.

    Gracefully degrades to disabled (returns empty hits, logs a debug message)
    when neither path provides a working setup — letting the pipeline run
    against Postgres-only sources without crashing in dev/test environments.
    """

    kind = "qdrant"

    def __init__(
        self,
        *,
        name: str,
        collection: str | None = None,
        store: Any = None,
        qdrant_client: Any = None,
        embedder: Any = None,
    ) -> None:
        self.name = name
        self.collection = collection
        self._store = store
        self._client = qdrant_client
        self._embedder = embedder
        self._enabled = store is not None or (qdrant_client is not None and collection is not None)

        if self._store is None and self._enabled:
            try:
                from ontology_store.vector import QdrantDocumentStore
                self._store = QdrantDocumentStore(
                    qdrant_client=self._client,
                    collection_name=collection,  # type: ignore[arg-type]
                    embedder=self._embedder,
                )
            except ImportError as exc:
                logger.warning(
                    "QdrantSource %r: cannot import ontology-store[vector]: %s. Source disabled.",
                    name, exc,
                )
                self._enabled = False

    @property
    def store(self) -> Any:
        return self._store

    def search(
        self,
        *,
        query_text: str | None = None,
        query_vector: list[float] | None = None,
        payload_filter: dict[str, Any] | None = None,
        k: int = 10,
    ) -> list[dict[str, Any]]:
        """Search returns a list of dicts (id, content, score, payload).

        Empty list when the source is disabled (no client / no store), so
        callers can degrade gracefully without exceptions.
        """
        if not self._enabled or self._store is None:
            logger.debug(
                "QdrantSource %r is disabled (no client/store); returning empty results", self.name,
            )
            return []
        hits = self._store.search(
            query_text=query_text,
            query_vector=query_vector,
            where=payload_filter,
            k=k,
        )
        return [
            {"id": h.id, "content": h.content, "score": h.score, "payload": h.payload}
            for h in hits
        ]


# ───────────────────────────────────────────────────────────────────────────
# Source factory — driven by PipelineConfig
# ───────────────────────────────────────────────────────────────────────────

def build_source(
    *,
    cfg: "ontology_retrieval.pipeline.config.SourceConfig",  # type: ignore[name-defined]
    database: Database | None,
    qdrant_client: Any | None = None,
    embedder: Any | None = None,
) -> Source:
    """Instantiate a concrete source given config + injected backends.

    For qdrant sources:
      - When `qdrant_client` is provided, the source wires it through.
      - When `qdrant_client` is None AND options.auto_client is true (default),
        the source uses `QdrantClientFactory.get()` which reads QDRANT_URL /
        QDRANT_HOST env vars.
      - When neither path yields a client, the source is disabled (returns
        empty results on search) but does not raise — allowing the pipeline to
        run Postgres-only in dev environments.
    """
    if cfg.kind in ("postgres_asset", "postgres_lineage", "postgres_annotation"):
        if database is None:
            raise RuntimeError(f"Source {cfg.name!r} (kind={cfg.kind!r}) requires a Database instance")
        if cfg.kind == "postgres_asset":
            return PostgresAssetSource(database=database, name=cfg.name)
        if cfg.kind == "postgres_lineage":
            return PostgresLineageSource(database=database, name=cfg.name)
        raise NotImplementedError(f"Source kind {cfg.kind!r} not implemented in v1")
    if cfg.kind == "qdrant":
        collection = cfg.options.get("collection")
        if not collection:
            raise ValueError(f"Source {cfg.name!r} (qdrant) requires options.collection")

        client = qdrant_client
        if client is None and cfg.options.get("auto_client", True):
            try:
                from ontology_store.vector import QdrantClientFactory
                client = QdrantClientFactory.get(
                    url=cfg.options.get("url"),
                    host=cfg.options.get("host"),
                    port=cfg.options.get("port", 6333),
                )
            except Exception as exc:
                logger.warning(
                    "Source %r: auto-build Qdrant client failed (%s); source will be disabled",
                    cfg.name, exc,
                )
                client = None

        # Embedder fallback
        local_embedder = embedder
        if local_embedder is None and cfg.options.get("auto_embedder", True) and client is not None:
            try:
                from ontology_store.vector import OpenAIEmbedder
                local_embedder = OpenAIEmbedder(
                    model=cfg.options.get("embed_model", "text-embedding-3-small"),
                )
            except Exception as exc:
                logger.warning(
                    "Source %r: auto-build embedder failed (%s); source will be disabled",
                    cfg.name, exc,
                )
                client = None  # disable without embedder

        return QdrantSource(
            name=cfg.name,
            collection=collection,
            qdrant_client=client,
            embedder=local_embedder,
        )
    if cfg.kind == "filesystem":
        raise NotImplementedError("Filesystem source not implemented in v1")
    raise ValueError(f"Unknown source kind: {cfg.kind!r}")
