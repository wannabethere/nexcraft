"""HierarchyVectorIndexer — high-level upserts into ontology collections.

Per `hierarchy_persistence_and_ingestion_spec.md` §5, each tier's collection
has a specific narrative-text source + payload-filter set. This class centralizes
that knowledge so callers (the live-sync reindex worker, the bootstrap script,
the retrieval source) don't have to remember the right composition per tier.

Usage:

    indexer = HierarchyVectorIndexer(client=qdrant_client, embedder=embedder, env="prod")
    indexer.ensure_all_env_collections()              # one-time bootstrap

    # Per-tier upserts:
    indexer.upsert_asset(asset_rk, narrative_text, payload)            # T4
    indexer.upsert_field(field_rk, narrative_text, payload)            # T5
    indexer.upsert_card(tenant_id, point_id, body, payload)            # cards_<tenant>
    indexer.upsert_sql_pair(tenant_id, sql_pair_id, question, payload) # sql_pairs_<tenant>
    indexer.upsert_historical_qa(tenant_id, qa_id, question, payload)  # historical_qa_<tenant>

Search helpers mirror the same per-tier shape:

    indexer.search_assets(query, payload_filter=..., k=10)
    indexer.search_cards(tenant_id, query, payload_filter=..., k=10)
"""
from __future__ import annotations

import logging
from typing import Any

from ontology_store.vector.collections import (
    CARD_EVENTS,
    CARDS,
    CAUSAL_EVENTS,
    CollectionSpec,
    HIER_T0_ORGS,
    HIER_T1_SOURCES,
    HIER_T2_CATALOGS,
    HIER_T3_SCHEMAS,
    HIER_T4_ASSETS,
    HIER_T5_FIELDS,
    HIER_T6_CODES,
    HISTORICAL_QA,
    PROTECTION_EVENTS,
    RELATION_EVENTS,
    SQL_PAIRS,
    resolve_collection_name,
)
from ontology_store.vector.embeddings import Embedder
from ontology_store.vector.events import EventEnvelope, EventKind
from ontology_store.vector.store import QdrantDocumentStore, SearchHit

logger = logging.getLogger(__name__)


class HierarchyVectorIndexer:
    """Per-tier upsert + search composing the right collection name + payload."""

    def __init__(
        self,
        *,
        qdrant_client: Any,
        embedder: Embedder,
        env: str = "prod",
    ) -> None:
        self.client = qdrant_client
        self.embedder = embedder
        self.env = env
        self._stores: dict[str, QdrantDocumentStore] = {}

    # ── Setup ───────────────────────────────────────────────────────────

    def ensure_all_env_collections(self) -> list[str]:
        """Create all env-scoped collections (hier_t0..t6) if missing.

        Per-tenant collections (cards, sql_pairs, historical_qa) are created
        on first use via the tenant-aware helpers.
        """
        created: list[str] = []
        for spec in (HIER_T0_ORGS, HIER_T1_SOURCES, HIER_T2_CATALOGS, HIER_T3_SCHEMAS,
                     HIER_T4_ASSETS, HIER_T5_FIELDS, HIER_T6_CODES):
            store = self._get_or_make_store(spec, tenant_id=None)
            existed = store.collection_exists()
            store.ensure_collection(vector_size=self.embedder.dim, distance=spec.distance)
            if not existed:
                created.append(store.collection_name)
        return created

    def ensure_tenant_collections(self, tenant_id: str) -> list[str]:
        """Create the seven per-tenant collections for `tenant_id` if missing.

        Three doc-per-row (cards, sql_pairs, historical_qa) + four event-sourced
        logs (causal_events, relation_events, protection_events, card_events).
        """
        created: list[str] = []
        for spec in (
            CARDS, SQL_PAIRS, HISTORICAL_QA,
            CAUSAL_EVENTS, RELATION_EVENTS, PROTECTION_EVENTS, CARD_EVENTS,
        ):
            store = self._get_or_make_store(spec, tenant_id=tenant_id)
            existed = store.collection_exists()
            store.ensure_collection(vector_size=self.embedder.dim, distance=spec.distance)
            if not existed:
                created.append(store.collection_name)
        return created

    # ── Env-scoped upserts (one method per tier) ────────────────────────

    def upsert_org(self, org_id: str, text: str, payload: dict[str, Any]) -> None:
        self._upsert_one(HIER_T0_ORGS, point_id=org_id, text=text, payload=payload)

    def upsert_source(self, source_id: str, text: str, payload: dict[str, Any]) -> None:
        self._upsert_one(HIER_T1_SOURCES, point_id=source_id, text=text, payload=payload)

    def upsert_catalog(self, catalog_uid: str, text: str, payload: dict[str, Any]) -> None:
        self._upsert_one(HIER_T2_CATALOGS, point_id=catalog_uid, text=text, payload=payload)

    def upsert_schema(self, schema_rk: str, text: str, payload: dict[str, Any]) -> None:
        self._upsert_one(HIER_T3_SCHEMAS, point_id=schema_rk, text=text, payload=payload)

    def upsert_asset(self, asset_rk: str, text: str, payload: dict[str, Any]) -> None:
        """Upsert a T4 asset (table/view/api_endpoint/function/metric).

        payload SHOULD include at least: asset_kind, lifecycle_stage, org_id,
        source_id, concepts, key_areas, causal_relations.
        """
        self._upsert_one(HIER_T4_ASSETS, point_id=asset_rk, text=text, payload=payload)

    def upsert_field(self, field_rk: str, text: str, payload: dict[str, Any]) -> None:
        """Upsert a T5 field. payload SHOULD include field_kind + parent_rk."""
        self._upsert_one(HIER_T5_FIELDS, point_id=field_rk, text=text, payload=payload)

    def upsert_code(self, code_rk: str, text: str, payload: dict[str, Any]) -> None:
        self._upsert_one(HIER_T6_CODES, point_id=code_rk, text=text, payload=payload)

    # ── Per-tenant upserts ──────────────────────────────────────────────

    def upsert_card(
        self, tenant_id: str, *, point_id: str, body: str, payload: dict[str, Any],
    ) -> None:
        """payload SHOULD include layer, kind, markings, refs, origin."""
        self._upsert_one(CARDS, point_id=point_id, text=body, payload=payload, tenant_id=tenant_id)

    def upsert_sql_pair(
        self, tenant_id: str, *, sql_pair_id: str, question: str, payload: dict[str, Any],
    ) -> None:
        """payload SHOULD include references_asset_rks, concepts, key_areas, source_provenance."""
        self._upsert_one(SQL_PAIRS, point_id=sql_pair_id, text=question, payload=payload, tenant_id=tenant_id)

    def upsert_historical_qa(
        self, tenant_id: str, *, qa_id: str, question: str, payload: dict[str, Any],
    ) -> None:
        """payload SHOULD include cited_asset_rks, used_intent, satisfaction, asked_at."""
        self._upsert_one(HISTORICAL_QA, point_id=qa_id, text=question, payload=payload, tenant_id=tenant_id)

    # ── Event-sourced append helpers ────────────────────────────────────
    #
    # Event collections are APPEND-ONLY. The point_id is the event_id from
    # the envelope (time-sortable). Never call upsert against an existing
    # event_id — emit a new event whose `supersedes` field points at the
    # one being corrected.

    def append_event(
        self,
        tenant_id: str,
        *,
        envelope: EventEnvelope,
        narrative: str,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        """Append one event to the appropriate event-sourced collection.

        Routing is by `envelope.event_kind`:
          - CAUSAL_*  → CAUSAL_EVENTS
          - RELATION_* / PREDICATE_* → RELATION_EVENTS
          - DATA_PROTECTION_* / PII_* / SENSITIVITY_* → PROTECTION_EVENTS
          - CARD_*    → CARD_EVENTS

        Args:
            tenant_id: scopes the destination collection.
            envelope:  EventEnvelope; `event_id` becomes the Qdrant point id.
            narrative: text that gets embedded (used for semantic search).
            extra_payload: optional additional payload keys hoisted on top of
                what the envelope provides. Use for event-kind-specific
                searchable fields (e.g. `evidence_count_bucket`).
        """
        spec = self._collection_for_event(envelope.event_kind)
        payload = envelope.to_qdrant_payload()
        if extra_payload:
            payload.update(extra_payload)
        # Use the event_id as the point id so a duplicate emit is a no-op
        # at the Qdrant level (same point, same vector — last write wins, but
        # event_ids are unique enough that this is effectively never hit).
        self._upsert_one(
            spec, point_id=envelope.event_id,
            text=narrative, payload=payload, tenant_id=tenant_id,
        )

    def append_causal_event(
        self, tenant_id: str, *,
        envelope: EventEnvelope, narrative: str,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        """Explicit CAUSAL_EVENTS append. Useful when the caller wants to
        guarantee the routing without relying on the event_kind mapping."""
        self._assert_event_kind(envelope, CAUSAL_EVENTS)
        self._upsert_one(
            CAUSAL_EVENTS, point_id=envelope.event_id,
            text=narrative,
            payload={**envelope.to_qdrant_payload(), **(extra_payload or {})},
            tenant_id=tenant_id,
        )

    def append_relation_event(
        self, tenant_id: str, *,
        envelope: EventEnvelope, narrative: str,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        self._assert_event_kind(envelope, RELATION_EVENTS)
        self._upsert_one(
            RELATION_EVENTS, point_id=envelope.event_id,
            text=narrative,
            payload={**envelope.to_qdrant_payload(), **(extra_payload or {})},
            tenant_id=tenant_id,
        )

    def append_protection_event(
        self, tenant_id: str, *,
        envelope: EventEnvelope, narrative: str,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        self._assert_event_kind(envelope, PROTECTION_EVENTS)
        self._upsert_one(
            PROTECTION_EVENTS, point_id=envelope.event_id,
            text=narrative,
            payload={**envelope.to_qdrant_payload(), **(extra_payload or {})},
            tenant_id=tenant_id,
        )

    def append_card_event(
        self, tenant_id: str, *,
        envelope: EventEnvelope, narrative: str,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        self._assert_event_kind(envelope, CARD_EVENTS)
        self._upsert_one(
            CARD_EVENTS, point_id=envelope.event_id,
            text=narrative,
            payload={**envelope.to_qdrant_payload(), **(extra_payload or {})},
            tenant_id=tenant_id,
        )

    # ── Event search ────────────────────────────────────────────────────

    def search_causal_events(
        self, tenant_id: str, query: str, *, where=None, k: int = 20,
    ) -> list[SearchHit]:
        return self._get_or_make_store(CAUSAL_EVENTS, tenant_id).search(
            query_text=query, where=where, k=k,
        )

    def search_relation_events(
        self, tenant_id: str, query: str, *, where=None, k: int = 20,
    ) -> list[SearchHit]:
        return self._get_or_make_store(RELATION_EVENTS, tenant_id).search(
            query_text=query, where=where, k=k,
        )

    def search_protection_events(
        self, tenant_id: str, query: str, *, where=None, k: int = 20,
    ) -> list[SearchHit]:
        return self._get_or_make_store(PROTECTION_EVENTS, tenant_id).search(
            query_text=query, where=where, k=k,
        )

    def search_card_events(
        self, tenant_id: str, query: str, *, where=None, k: int = 20,
    ) -> list[SearchHit]:
        return self._get_or_make_store(CARD_EVENTS, tenant_id).search(
            query_text=query, where=where, k=k,
        )

    # ── Search helpers (one per tier) ───────────────────────────────────

    def search_orgs(self, query: str, *, where: dict[str, Any] | None = None, k: int = 10) -> list[SearchHit]:
        return self._get_or_make_store(HIER_T0_ORGS, None).search(query_text=query, where=where, k=k)

    def search_sources(self, query: str, *, where=None, k=10) -> list[SearchHit]:
        return self._get_or_make_store(HIER_T1_SOURCES, None).search(query_text=query, where=where, k=k)

    def search_catalogs(self, query: str, *, where=None, k=10) -> list[SearchHit]:
        return self._get_or_make_store(HIER_T2_CATALOGS, None).search(query_text=query, where=where, k=k)

    def search_schemas(self, query: str, *, where=None, k=10) -> list[SearchHit]:
        return self._get_or_make_store(HIER_T3_SCHEMAS, None).search(query_text=query, where=where, k=k)

    def search_assets(self, query: str, *, where=None, k=10) -> list[SearchHit]:
        return self._get_or_make_store(HIER_T4_ASSETS, None).search(query_text=query, where=where, k=k)

    def search_fields(self, query: str, *, where=None, k=10) -> list[SearchHit]:
        return self._get_or_make_store(HIER_T5_FIELDS, None).search(query_text=query, where=where, k=k)

    def search_codes(self, query: str, *, where=None, k=10) -> list[SearchHit]:
        return self._get_or_make_store(HIER_T6_CODES, None).search(query_text=query, where=where, k=k)

    def search_cards(self, tenant_id: str, query: str, *, where=None, k=10) -> list[SearchHit]:
        return self._get_or_make_store(CARDS, tenant_id).search(query_text=query, where=where, k=k)

    def search_sql_pairs(self, tenant_id: str, query: str, *, where=None, k=10) -> list[SearchHit]:
        return self._get_or_make_store(SQL_PAIRS, tenant_id).search(query_text=query, where=where, k=k)

    def search_historical_qa(self, tenant_id: str, query: str, *, where=None, k=10) -> list[SearchHit]:
        return self._get_or_make_store(HISTORICAL_QA, tenant_id).search(query_text=query, where=where, k=k)

    # ── Store-accessor (for callers that need the underlying store) ─────

    def store_for(self, spec: CollectionSpec, *, tenant_id: str | None = None) -> QdrantDocumentStore:
        return self._get_or_make_store(spec, tenant_id)

    # ── Internals ───────────────────────────────────────────────────────

    def _get_or_make_store(self, spec: CollectionSpec, tenant_id: str | None) -> QdrantDocumentStore:
        name = resolve_collection_name(spec, env=self.env, tenant_id=tenant_id)
        cached = self._stores.get(name)
        if cached is not None:
            return cached
        store = QdrantDocumentStore(
            qdrant_client=self.client,
            collection_name=name,
            embedder=self.embedder,
        )
        # Lazy create on first use
        if not store.collection_exists():
            store.ensure_collection(vector_size=self.embedder.dim, distance=spec.distance)
        self._stores[name] = store
        return store

    def _upsert_one(
        self,
        spec: CollectionSpec,
        *,
        point_id: str,
        text: str,
        payload: dict[str, Any],
        tenant_id: str | None = None,
    ) -> None:
        store = self._get_or_make_store(spec, tenant_id)
        # All payload keys are stored under metadata.* so filters match the
        # build_qdrant_filter convention.
        store.upsert_points([{"id": point_id, "text": text, "metadata": payload}])

    # ── Event routing ───────────────────────────────────────────────────

    _EVENT_KIND_TO_SPEC: dict[str, CollectionSpec] = {}  # filled lazily on first call

    def _collection_for_event(self, kind: EventKind) -> CollectionSpec:
        """Map an EventKind to its destination collection.

        Built once, lazily, the first time any event is appended. Membership
        is declared on `EventKind.for_collection()`.
        """
        if not self._EVENT_KIND_TO_SPEC:
            for spec in (CAUSAL_EVENTS, RELATION_EVENTS, PROTECTION_EVENTS, CARD_EVENTS):
                for ek in EventKind.for_collection(spec.tier_id):
                    self._EVENT_KIND_TO_SPEC[ek.value] = spec
        try:
            return self._EVENT_KIND_TO_SPEC[kind.value]
        except KeyError as exc:
            raise ValueError(
                f"Event kind {kind!r} has no destination collection. "
                "Add it to `EventKind.for_collection()` mapping."
            ) from exc

    @staticmethod
    def _assert_event_kind(envelope: EventEnvelope, spec: CollectionSpec) -> None:
        """Verify the envelope's kind belongs in `spec`. Raises if not."""
        allowed = set(EventKind.for_collection(spec.tier_id))
        if envelope.event_kind not in allowed:
            raise ValueError(
                f"Event kind {envelope.event_kind!r} does not belong in "
                f"collection {spec.tier_id!r}. Allowed: {[k.value for k in allowed]}"
            )
