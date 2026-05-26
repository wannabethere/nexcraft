"""QdrantDocumentStore — legacy-compatible interface + ontology-aware extensions.

Mirrors the surface of `genieml/agents/app/storage/qdrant_store.DocumentQdrantStore`
so existing indexing processors (DBSchema, TableDescription, etc.) work after
an import-only swap. Additions on top:

  - `upsert_points(...)` is the structured-payload primary path used by the
    HierarchyVectorIndexer (no LangChain Document wrapping required).
  - `search(...)` is the structured-input primary path used by retrieval kinds
    (returns `SearchHit` Pydantic objects).
  - `count()` / `delete_all()` / `delete_by_filter(filter_dict)` helpers.
  - The Chroma-style `.collection` adapter is kept for legacy `delete(where=...)`
    and `get(where=...)` calls.
  - Filter-builder supports legacy `{"$and": [...]}`, `{"$eq": ...}`, `{"$in": ...}`
    AND ontology-style flat filters `{"asset_kind": "table", "concepts": ["employee"]}`
    (list values → MatchAny semantics).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from ontology_store.vector.embeddings import Embedder

logger = logging.getLogger(__name__)

try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Distance,
        FieldCondition,
        Filter,
        MatchAny,
        MatchValue,
        PointIdsList,
        PointStruct,
        VectorParams,
    )
    try:
        from langchain_qdrant import QdrantVectorStore as LangchainQdrant
    except ImportError:
        try:
            from langchain_qdrant import Qdrant as LangchainQdrant
        except ImportError:
            LangchainQdrant = None  # type: ignore[assignment]
    _QDRANT_OK = True
except ImportError:
    _QDRANT_OK = False
    QdrantClient = None  # type: ignore[assignment]
    LangchainQdrant = None  # type: ignore[assignment]
    Distance = None  # type: ignore[assignment]
    PointStruct = None  # type: ignore[assignment]
    FieldCondition = None  # type: ignore[assignment]
    Filter = None  # type: ignore[assignment]
    MatchAny = None  # type: ignore[assignment]
    MatchValue = None  # type: ignore[assignment]
    PointIdsList = None  # type: ignore[assignment]
    VectorParams = None  # type: ignore[assignment]


# ── Public Pydantic types ───────────────────────────────────────────────

class SearchHit(BaseModel):
    """One result from semantic_search / search."""
    id: str
    content: str = ""
    score: float
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def metadata(self) -> dict[str, Any]:
        """Compat alias — legacy callers use .metadata."""
        return self.payload.get("metadata") if isinstance(self.payload, dict) else {} or self.payload


# ── Helper: build idempotent Qdrant point id from any string ────────────

def to_qdrant_point_id(s: str) -> str:
    """Idempotent point id. Accepts UUIDs as-is; otherwise UUIDv5 over namespace."""
    try:
        uuid.UUID(s)
        return s
    except (ValueError, AttributeError, TypeError):
        pass
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, "qdrant.point." + s))


# ── Filter builder ──────────────────────────────────────────────────────

def build_qdrant_filter(where: Optional[dict[str, Any]]) -> Optional["Filter"]:
    """Translate a legacy-style or flat dict filter into a Qdrant `Filter`.

    Supports:
      - {"$and": [ ... ]}                                  → AND of sub-filters
      - {"key": "value"}                                   → metadata.key == value
      - {"key": {"$eq": "value"}}                          → metadata.key == value
      - {"key": {"$in": [v1, v2]}}                         → metadata.key in [v1, v2]
      - {"key": [v1, v2]}                                  → metadata.key MatchAny ([v1, v2])

    Keys are namespaced under `metadata.` automatically — matching the storage
    convention used by both this store and the legacy DocumentQdrantStore.
    """
    if not _QDRANT_OK:
        return None
    if not where:
        return None

    conditions: list[Any] = []
    _collect_conditions(where, conditions)
    if not conditions:
        return None
    return Filter(must=conditions)


def _collect_conditions(where: dict[str, Any], out: list[Any]) -> None:
    if "$and" in where:
        for sub in where["$and"]:
            if isinstance(sub, dict):
                _collect_conditions(sub, out)
        return

    for key, value in where.items():
        if key.startswith("$"):
            continue
        # Auto-namespace under metadata.* if caller didn't.
        qkey = key if key.startswith("metadata.") else f"metadata.{key}"

        if isinstance(value, dict):
            if "$eq" in value:
                out.append(FieldCondition(key=qkey, match=MatchValue(value=value["$eq"])))
            elif "$in" in value:
                in_values = value["$in"] if isinstance(value["$in"], list) else [value["$in"]]
                out.append(FieldCondition(key=qkey, match=MatchAny(any=in_values)))
            elif "$ne" in value:
                # Approximate via must_not — caller wraps as needed; we just skip here.
                logger.debug("$ne operator not yet supported in build_qdrant_filter; skipping key %s", key)
            else:
                # nested dict without known operator — skip
                logger.debug("Unsupported filter shape for key %s: %s", key, value)
        elif isinstance(value, list):
            # ANY-overlap semantics for list-valued payload (e.g., concepts, key_areas)
            out.append(FieldCondition(key=qkey, match=MatchAny(any=value)))
        else:
            out.append(FieldCondition(key=qkey, match=MatchValue(value=value)))


# ── Collection adapter (legacy .collection.delete / get) ───────────────

class QdrantCollectionAdapter:
    """Chroma-like `.collection` wrapper for legacy callers."""

    def __init__(self, client: "QdrantClient", collection_name: str):
        self._client = client
        self._name = collection_name

    def delete(self, where: Optional[dict] = None, ids: Optional[list[str]] = None) -> None:
        if ids:
            self._client.delete(
                collection_name=self._name,
                points_selector=PointIdsList(points=[to_qdrant_point_id(i) for i in ids]),
            )
            return
        f = build_qdrant_filter(where)
        if f is not None:
            scroll_result = self._client.scroll(
                collection_name=self._name, scroll_filter=f, limit=10_000,
            )
            point_ids = [p.id for p in scroll_result[0]]
            if point_ids:
                self._client.delete(
                    collection_name=self._name,
                    points_selector=PointIdsList(points=point_ids),
                )
            return
        # No filter → delete all (scrolled batches)
        offset = None
        while True:
            result, offset = self._client.scroll(
                collection_name=self._name, limit=200, offset=offset,
            )
            if not result:
                break
            self._client.delete(
                collection_name=self._name,
                points_selector=PointIdsList(points=[p.id for p in result]),
            )
            if offset is None:
                break

    def get(self, where: Optional[dict] = None, **_: Any) -> dict[str, Any]:
        f = build_qdrant_filter(where)
        result, _next = self._client.scroll(
            collection_name=self._name, scroll_filter=f, limit=10_000,
        )
        return {
            "ids": [str(p.id) for p in result],
            "metadatas": [(p.payload or {}) for p in result],
        }

    def count(self, where: Optional[dict] = None) -> int:
        f = build_qdrant_filter(where)
        result = self._client.count(collection_name=self._name, count_filter=f, exact=True)
        return int(getattr(result, "count", 0))


# ── Main store ──────────────────────────────────────────────────────────

@dataclass
class _UpsertPoint:
    """Internal — what `upsert_points` accepts."""
    id: str
    text: str
    metadata: dict[str, Any]
    page_content: str | None = None  # falls back to `text` if absent


class QdrantDocumentStore:
    """Qdrant document store. Drop-in for legacy `DocumentQdrantStore` plus extras."""

    def __init__(
        self,
        *,
        qdrant_client: Optional["QdrantClient"] = None,
        collection_name: str,
        embedder: Optional[Embedder] = None,
        host: Optional[str] = None,
        port: int = 6333,
        batch_size: int = 200,
    ) -> None:
        if not _QDRANT_OK:
            raise ImportError(
                "Qdrant dependencies missing. Install with 'ontology-store[vector]'."
            )
        if qdrant_client is None:
            from ontology_store.vector.client import QdrantClientFactory
            qdrant_client = QdrantClientFactory.get(host=host or "localhost", port=port)

        self.client = qdrant_client
        self.collection_name = collection_name
        self.embedder = embedder
        self.batch_size = batch_size
        self.collection = QdrantCollectionAdapter(qdrant_client, collection_name)

        self._vectorstore = None  # lazy LangChain wrapper for legacy add_documents path

    # ── Collection lifecycle ────────────────────────────────────────────

    def ensure_collection(self, *, vector_size: int | None = None, distance: str = "Cosine") -> None:
        """Create the collection if it doesn't exist."""
        if self.client.collection_exists(self.collection_name):
            return
        size = vector_size or (self.embedder.dim if self.embedder is not None else 1536)
        dist = getattr(Distance, distance.upper(), Distance.COSINE)
        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config=VectorParams(size=size, distance=dist),
        )
        logger.info("Created Qdrant collection %s (dim=%d, distance=%s)", self.collection_name, size, distance)

    def collection_exists(self) -> bool:
        return self.client.collection_exists(self.collection_name)

    def drop(self) -> None:
        if self.client.collection_exists(self.collection_name):
            self.client.delete_collection(self.collection_name)
            logger.info("Dropped Qdrant collection %s", self.collection_name)

    # ── Primary write path: structured points ───────────────────────────

    def upsert_points(
        self,
        points: Iterable[dict[str, Any]],
        *,
        log_schema: bool = False,
    ) -> dict[str, Any]:
        """Upsert structured points.

        Each item must have:
          - 'text' (required) — used to compute the embedding
          - 'metadata' (optional dict) — stored in payload under 'metadata'
          - 'id' (optional) — point id; auto-generated if absent
          - 'page_content' (optional) — content stored under payload.page_content;
            falls back to 'text' if absent
        """
        if self.embedder is None:
            raise RuntimeError(
                "Embedder is required for upsert_points; pass embedder= to the constructor"
            )
        items = list(points)
        if not items:
            return {"documents_written": 0}

        texts = [it["text"] for it in items]
        vectors = self.embedder.embed_documents(texts)

        qpoints = []
        for it, vec in zip(items, vectors):
            point_id = to_qdrant_point_id(it.get("id") or str(uuid4()))
            payload = {
                "metadata": it.get("metadata") or {},
                "page_content": it.get("page_content") or it["text"],
                "text": it["text"],
            }
            if log_schema:
                logger.debug("Qdrant point payload (%s): %s", point_id, payload)
            qpoints.append(PointStruct(id=point_id, vector=vec, payload=payload))

        added = 0
        for start in range(0, len(qpoints), self.batch_size):
            batch = qpoints[start : start + self.batch_size]
            try:
                self.client.upsert(collection_name=self.collection_name, points=batch)
                added += len(batch)
            except Exception as exc:
                logger.error("Qdrant upsert failed at offset %d: %s", start, exc)

        return {"documents_written": added}

    # ── Legacy compatible add_documents ─────────────────────────────────

    def add_documents(self, docs: list[Any]) -> dict[str, int]:
        """Legacy-shape add for LangchainDocument / {"metadata", "data"} inputs.

        Internally translates to `upsert_points`. For new code, prefer
        `upsert_points` directly — it avoids the LangChain Document round-trip.
        """
        if not docs:
            return {"documents_written": 0}

        points: list[dict[str, Any]] = []

        # Lazy LangChain import
        try:
            from langchain_core.documents import Document as LangchainDocument
        except ImportError:
            LangchainDocument = None  # type: ignore[assignment]

        for d in docs:
            if LangchainDocument is not None and isinstance(d, LangchainDocument):
                points.append({
                    "id": d.metadata.get("id"),
                    "text": d.page_content,
                    "metadata": dict(d.metadata or {}),
                    "page_content": d.page_content,
                })
                continue
            if isinstance(d, dict) and "metadata" in d and "data" in d:
                data = d["data"]
                if isinstance(data, str):
                    text = data
                elif isinstance(data, dict):
                    text = data.get("content") or data.get("text") or str(data)
                else:
                    text = str(data)
                points.append({
                    "id": (d.get("metadata") or {}).get("id"),
                    "text": text,
                    "metadata": dict(d.get("metadata") or {}),
                    "page_content": text,
                })
                continue
            logger.warning("Skipping invalid document for add_documents: %s", type(d))

        return self.upsert_points(points)

    # ── Primary read path: structured search ────────────────────────────

    def search(
        self,
        *,
        query_text: str | None = None,
        query_vector: list[float] | None = None,
        where: Optional[dict[str, Any]] = None,
        k: int = 10,
    ) -> list[SearchHit]:
        """Semantic search. Provide either `query_text` (embed in-process) or `query_vector`."""
        if not self.client.collection_exists(self.collection_name):
            logger.debug("search: collection %s does not exist; returning empty", self.collection_name)
            return []

        if query_vector is None:
            if query_text is None or not query_text.strip():
                logger.debug("search called without query_text or query_vector; returning empty")
                return []
            if self.embedder is None:
                raise RuntimeError("Embedder is required when query_vector is not provided")
            query_vector = self.embedder.embed_query(query_text)

        flt = build_qdrant_filter(where)
        try:
            results = self.client.search(
                collection_name=self.collection_name,
                query_vector=query_vector,
                query_filter=flt,
                limit=k,
                with_payload=True,
            )
        except Exception as exc:
            logger.error("Qdrant search error in %s: %s", self.collection_name, exc)
            return []

        hits: list[SearchHit] = []
        for r in results:
            payload = dict(r.payload or {})
            content = payload.get("page_content") or payload.get("text") or ""
            hits.append(SearchHit(
                id=str(r.id),
                content=content,
                score=float(r.score),
                payload=payload,
            ))
        return hits

    # ── Legacy semantic_search (returns list[dict]) ─────────────────────

    def semantic_search(
        self,
        query: str,
        k: int = 5,
        where: Optional[dict[str, Any]] = None,
        query_embedding: Optional[list[float]] = None,
    ) -> list[dict[str, Any]]:
        """Legacy-shaped wrapper around `search`. Returns list of dicts.

        Each dict: {"content", "metadata", "score", "id"}.
        """
        hits = self.search(query_text=query, query_vector=query_embedding, where=where, k=k)
        out: list[dict[str, Any]] = []
        for h in hits:
            meta = h.payload.get("metadata") if isinstance(h.payload, dict) else None
            if not isinstance(meta, dict):
                meta = {}
            # Hoist non-metadata payload fields (page_content, text) to merge structure
            out.append({
                "content": h.content,
                "metadata": meta,
                "score": h.score,
                "id": meta.get("id") or h.id,
            })
        return out

    # ── Deletes ─────────────────────────────────────────────────────────

    def delete_by_filter(self, where: dict[str, Any]) -> int:
        """Delete every point matching the filter. Returns count deleted."""
        f = build_qdrant_filter(where)
        if f is None:
            return 0
        scroll_result = self.client.scroll(
            collection_name=self.collection_name, scroll_filter=f, limit=10_000,
        )
        point_ids = [p.id for p in scroll_result[0]]
        if not point_ids:
            return 0
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=PointIdsList(points=point_ids),
        )
        return len(point_ids)

    def delete_by_project_id(self, project_id: str) -> dict[str, Any]:
        """Legacy compat — deletes by `metadata.project_id == project_id`."""
        n = self.delete_by_filter({"project_id": project_id})
        logger.info("Deleted %d points for project_id=%s in %s", n, project_id, self.collection_name)
        return {"documents_deleted": n}

    def delete_by_ids(self, ids: list[str]) -> int:
        """Delete points by their (raw or original) ids; mapped through to_qdrant_point_id."""
        if not ids:
            return 0
        qids = [to_qdrant_point_id(i) for i in ids]
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=PointIdsList(points=qids),
        )
        return len(qids)

    # ── Stats ───────────────────────────────────────────────────────────

    def count(self, where: Optional[dict[str, Any]] = None) -> int:
        f = build_qdrant_filter(where)
        result = self.client.count(collection_name=self.collection_name, count_filter=f, exact=True)
        return int(getattr(result, "count", 0))
