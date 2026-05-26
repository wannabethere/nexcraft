"""Tests for the event-sourced vector layer.

Three layers covered:
  - **EventEnvelope + EventKind** (pure Python, no Qdrant).
    Id generation, payload flattening, kind→collection routing.
  - **Narrative builders** (pure Python, no Qdrant).
    Translate Postgres row stand-ins into `(envelope, narrative, extra_payload)`.
  - **Indexer routing** (pure Python; uses a stub QdrantDocumentStore).
    Verifies `append_event` dispatches to the right collection by event_kind
    and that `_assert_event_kind` rejects mismatches.

Real Qdrant integration lives in `test_vector.py` (Qdrant-gated).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from ontology_store.vector.collections import (
    CARD_EVENTS,
    CAUSAL_EVENTS,
    PROTECTION_EVENTS,
    RELATION_EVENTS,
)
from ontology_store.vector.events import (
    COMMON_PAYLOAD_KEYS,
    EventEnvelope,
    EventKind,
)
from ontology_store.workers.event_narrative import (
    _bucket_count,
    build_card_event,
    build_causal_candidate_event,
    build_data_protection_event,
    build_pii_classification_event,
    build_predicate_attached_event,
    build_relation_type_event,
)


# ───────────────────────────────────────────────────────────────────────────
# EventEnvelope / EventKind
# ───────────────────────────────────────────────────────────────────────────


class TestEventEnvelope:
    def test_new_id_is_time_sortable(self):
        # Generate two ids 0.01s apart — the second should sort after the first
        # lexicographically.
        e1 = EventEnvelope.new_id(
            kind=EventKind.CAUSAL_CANDIDATE_PROPOSED,
            at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        )
        e2 = EventEnvelope.new_id(
            kind=EventKind.CAUSAL_CANDIDATE_PROPOSED,
            at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        )
        assert e1 < e2
        assert e1.startswith("evt_2026")

    def test_to_qdrant_payload_hoists_envelope_keys(self):
        env = EventEnvelope(
            event_id="evt_test",
            event_kind=EventKind.CAUSAL_CANDIDATE_VALIDATED,
            subject_rk="rk:asset",
            produced_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            provenance="stat_validator",
            run_id="run-123",
            confidence=0.87,
            payload={"predicate": "leading_indicator_of", "status": "validated"},
        )
        p = env.to_qdrant_payload()
        # Envelope keys
        assert p["event_id"] == "evt_test"
        assert p["event_kind"] == "causal_candidate_validated"
        assert p["produced_at"] == "2026-01-01T00:00:00+00:00"
        assert p["provenance"] == "stat_validator"
        assert p["confidence"] == 0.87
        # Payload keys hoisted (so they can be Qdrant payload-filter indexes)
        assert p["predicate"] == "leading_indicator_of"
        assert p["status"] == "validated"

    def test_supersedes_field_threads_through(self):
        env = EventEnvelope(
            event_id="evt_new",
            event_kind=EventKind.CAUSAL_CANDIDATE_REJECTED,
            subject_rk="rk:asset",
            produced_at=datetime.now(timezone.utc),
            provenance="human:reviewer",
            supersedes="evt_old",
        )
        assert env.to_qdrant_payload()["supersedes"] == "evt_old"


class TestEventKindRouting:
    def test_for_collection_returns_expected_kinds(self):
        causal = EventKind.for_collection("causal_events")
        assert EventKind.CAUSAL_CANDIDATE_PROPOSED in causal
        assert EventKind.CAUSAL_CANDIDATE_VALIDATED in causal
        assert EventKind.RELATION_TYPE_OBSERVED not in causal

        relations = EventKind.for_collection("relation_events")
        assert EventKind.PREDICATE_ATTACHED_TO_EDGE in relations
        assert EventKind.RELATION_TYPE_CANONICALIZED in relations
        assert EventKind.CARD_AUTHORED not in relations

    def test_unknown_collection_returns_empty(self):
        assert EventKind.for_collection("nope") == ()


# ───────────────────────────────────────────────────────────────────────────
# Causal narrative builder
# ───────────────────────────────────────────────────────────────────────────


class TestCausalNarrativeBuilder:
    def _row(self, **overrides: Any) -> SimpleNamespace:
        defaults: dict[str, Any] = dict(
            asset_rk="postgres://csod-pg/db/public/employee",
            subject_ref="postgres://csod-pg/db/public/employee.due_date",
            predicate="leading_indicator_of",
            object_ref="postgres://csod-pg/db/public/employee.completion",
            status="proposed",
            confidence=0.7,
            mechanism_hint="Overdue training drives attrition risk.",
            rationale="Tracks per-employee training cadence.",
            evidence_columns=["due_date", "employee_id"],
            provenance="llm_causal_dependency",
            created_at=datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc),
            validated_at=None,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_proposed_emits_proposed_kind(self):
        env, narrative, extra = build_causal_candidate_event(row=self._row())
        assert env.event_kind == EventKind.CAUSAL_CANDIDATE_PROPOSED
        assert env.subject_rk.endswith("/employee")
        assert env.confidence == 0.7
        assert "leading_indicator_of" in narrative
        assert "Overdue training" in narrative
        assert extra["evidence_columns_joined"] == "due_date, employee_id"

    def test_validated_emits_validated_kind(self):
        row = self._row(status="validated", validated_at=datetime(2026, 5, 18, 13, tzinfo=timezone.utc))
        env, _, _ = build_causal_candidate_event(row=row)
        assert env.event_kind == EventKind.CAUSAL_CANDIDATE_VALIDATED

    def test_rejected_emits_rejected_kind(self):
        env, _, _ = build_causal_candidate_event(row=self._row(status="rejected"))
        assert env.event_kind == EventKind.CAUSAL_CANDIDATE_REJECTED

    def test_inconclusive_emits_inconclusive_kind(self):
        env, _, _ = build_causal_candidate_event(row=self._row(status="inconclusive"))
        assert env.event_kind == EventKind.CAUSAL_CANDIDATE_INCONCLUSIVE

    def test_unknown_status_falls_back_to_proposed(self):
        env, _, _ = build_causal_candidate_event(row=self._row(status="bogus"))
        assert env.event_kind == EventKind.CAUSAL_CANDIDATE_PROPOSED

    def test_run_id_and_org_id_flow_into_payload(self):
        env, _, _ = build_causal_candidate_event(
            row=self._row(),
            run_id="ontology-ingestion-abc",
            org_id="csod",
            source_id="csod-pg",
        )
        p = env.to_qdrant_payload()
        assert p["run_id"] == "ontology-ingestion-abc"
        assert p["org_id"] == "csod"
        assert p["source_id"] == "csod-pg"


# ───────────────────────────────────────────────────────────────────────────
# Relation narrative builder
# ───────────────────────────────────────────────────────────────────────────


class TestRelationNarrativeBuilder:
    def test_basic_canonicalization_event(self):
        row = SimpleNamespace(
            predicate="references_entity",
            domain="Employee",
            range_type="Department",
            evidence_count=12,
            confidence=0.85,
            surfaces="references,fk_to",
            provenance="induce_schema",
            org_id="csod",
            created_at=datetime(2026, 5, 18, tzinfo=timezone.utc),
            updated_at=datetime(2026, 5, 18, tzinfo=timezone.utc),
        )
        env, narrative, extra = build_relation_type_event(row=row, run_id="r1")
        assert env.event_kind == EventKind.RELATION_TYPE_CANONICALIZED
        assert env.subject_rk == "references_entity:Employee->Department"
        assert "Employee" in narrative
        assert "Department" in narrative
        assert "12 edge" in narrative
        # evidence_count_bucket bucketed via _bucket_count
        p = env.to_qdrant_payload()
        assert p["evidence_count_bucket"] == "med"  # 6-50
        # Surfaces preserved for retrieval
        assert extra["surfaces_joined"] == "references,fk_to"

    def test_high_evidence_bucket(self):
        row = SimpleNamespace(
            predicate="p", domain="X", range_type="Y",
            evidence_count=100, confidence=None, surfaces=None,
            provenance="induce_schema", org_id="csod",
            created_at=None, updated_at=None,
        )
        env, _, _ = build_relation_type_event(row=row)
        assert env.to_qdrant_payload()["evidence_count_bucket"] == "high"

    def test_predicate_attached_event(self):
        env, narrative, _ = build_predicate_attached_event(
            from_rk="postgres://csod-pg/db/public/employee",
            to_rk="postgres://csod-pg/db/public/department",
            edge_kind="depends_on",
            predicate="references_entity",
            domain="Employee",
            range_type="Department",
            org_id="csod",
        )
        assert env.event_kind == EventKind.PREDICATE_ATTACHED_TO_EDGE
        assert "depends_on" in env.subject_rk
        assert "references_entity" in narrative

    def test_bucket_count_thresholds(self):
        assert _bucket_count(1) == "low"
        assert _bucket_count(5) == "low"
        assert _bucket_count(6) == "med"
        assert _bucket_count(50) == "med"
        assert _bucket_count(51) == "high"


# ───────────────────────────────────────────────────────────────────────────
# Data protection builder
# ───────────────────────────────────────────────────────────────────────────


class TestProtectionNarrativeBuilder:
    def test_proposed_hint(self):
        row = SimpleNamespace(
            asset_rk="postgres://csod-pg/db/public/users",
            rls_predicates=["user_id = current_setting('app.user_id')"],
            cls_columns=["ssn", "email"],
            rationale="Standard employee table — SSN must be masked.",
            provenance="llm_data_protection",
            status="proposed",
            created_at=None, updated_at=None,
        )
        env, narrative, extra = build_data_protection_event(row=row, org_id="csod")
        assert env.event_kind == EventKind.DATA_PROTECTION_HINT_PROPOSED
        assert "ssn" in narrative
        assert "current_setting" in narrative
        assert extra["cls_columns_joined"] == "ssn, email"

    def test_applied_status_changes_kind(self):
        row = SimpleNamespace(
            asset_rk="rk", rls_predicates=[], cls_columns=[],
            rationale=None, provenance="ops", status="applied",
            created_at=None, updated_at=None,
        )
        env, _, _ = build_data_protection_event(row=row)
        assert env.event_kind == EventKind.DATA_PROTECTION_HINT_APPLIED

    def test_pii_classification_event(self):
        env, narrative, _ = build_pii_classification_event(
            column_rk="postgres://csod-pg/db/public/users/ssn",
            is_pii=True,
            pii_categories=["government_id", "person"],
            sensitivity_class="restricted",
            org_id="csod",
        )
        assert env.event_kind == EventKind.PII_CLASSIFIED
        p = env.to_qdrant_payload()
        assert p["is_pii"] is True
        assert "government_id" in p["pii_categories"]
        assert p["sensitivity_class"] == "restricted"
        assert "is_pii=True" in narrative


# ───────────────────────────────────────────────────────────────────────────
# Card builder
# ───────────────────────────────────────────────────────────────────────────


class TestCardNarrativeBuilder:
    def test_new_card_emits_authored(self):
        row = SimpleNamespace(
            org_id="csod", kind="causal_node", card_id="compliance_gap",
            title="Compliance Gap",
            body="A measurable gap between current and required state.",
            aliases=["comp_gap"], deprecated=False,
            origin="tenant",
            created_at=None, updated_at=None,
        )
        env, narrative, extra = build_card_event(row=row, is_new=True)
        assert env.event_kind == EventKind.CARD_AUTHORED
        assert env.subject_rk == "csod:causal_node:compliance_gap"
        assert "Compliance Gap" in narrative
        assert extra["aliases_joined"] == "comp_gap"

    def test_existing_card_emits_revised(self):
        row = SimpleNamespace(
            org_id="csod", kind="object_type", card_id="employee",
            title=None, body="...", aliases=[], deprecated=False,
            origin="tenant", created_at=None, updated_at=None,
        )
        env, _, _ = build_card_event(row=row, is_new=False)
        assert env.event_kind == EventKind.CARD_REVISED

    def test_deprecated_card_emits_deprecated_kind(self):
        row = SimpleNamespace(
            org_id="csod", kind="causal_node", card_id="legacy",
            title=None, body="", aliases=[],
            deprecated=True, origin="tenant",
            created_at=None, updated_at=None,
        )
        env, _, _ = build_card_event(row=row, is_new=False)
        assert env.event_kind == EventKind.CARD_DEPRECATED


# ───────────────────────────────────────────────────────────────────────────
# Indexer routing — uses a stub store
# ───────────────────────────────────────────────────────────────────────────


class _StubStore:
    """Captures upsert calls in lieu of talking to Qdrant."""
    def __init__(self):
        self.upserts: list[dict[str, Any]] = []
        self.collection_name = "stub"

    def collection_exists(self): return True

    def ensure_collection(self, **kw): pass

    def upsert_points(self, points):
        self.upserts.extend(points)


@pytest.fixture
def stub_indexer(monkeypatch):
    """Build a HierarchyVectorIndexer wired to a stub store factory.

    The stub captures every upsert; we then assert on the collection name
    used to confirm routing was correct.
    """
    from ontology_store.vector import hierarchy

    captured: dict[str, _StubStore] = {}

    def _make_store(self, spec, tenant_id):
        from ontology_store.vector.collections import resolve_collection_name
        name = resolve_collection_name(spec, env=self.env, tenant_id=tenant_id)
        store = captured.setdefault(name, _StubStore())
        store.collection_name = name
        return store

    monkeypatch.setattr(
        hierarchy.HierarchyVectorIndexer, "_get_or_make_store", _make_store,
    )

    class _StubEmbedder:
        dim = 1536

    return hierarchy.HierarchyVectorIndexer(
        qdrant_client=None, embedder=_StubEmbedder(), env="test",
    ), captured


class TestIndexerRouting:
    def test_append_event_routes_causal_to_causal_collection(self, stub_indexer):
        indexer, captured = stub_indexer
        env = EventEnvelope(
            event_id="evt_1",
            event_kind=EventKind.CAUSAL_CANDIDATE_VALIDATED,
            subject_rk="rk",
            produced_at=datetime.now(timezone.utc),
            provenance="test",
        )
        indexer.append_event(
            tenant_id="acme", envelope=env, narrative="hello",
        )
        # Routed to causal_events_acme
        assert "causal_events_acme" in captured
        store = captured["causal_events_acme"]
        assert len(store.upserts) == 1
        assert store.upserts[0]["id"] == "evt_1"
        # Other collections untouched
        assert "relation_events_acme" not in captured

    def test_append_event_routes_relation_correctly(self, stub_indexer):
        indexer, captured = stub_indexer
        env = EventEnvelope(
            event_id="evt_2",
            event_kind=EventKind.RELATION_TYPE_CANONICALIZED,
            subject_rk="references:Employee->Department",
            produced_at=datetime.now(timezone.utc),
            provenance="test",
        )
        indexer.append_event(tenant_id="acme", envelope=env, narrative="...")
        assert "relation_events_acme" in captured

    def test_append_event_routes_card_correctly(self, stub_indexer):
        indexer, captured = stub_indexer
        env = EventEnvelope(
            event_id="evt_3", event_kind=EventKind.CARD_AUTHORED,
            subject_rk="acme:causal_node:x",
            produced_at=datetime.now(timezone.utc),
            provenance="test",
        )
        indexer.append_event(tenant_id="acme", envelope=env, narrative="...")
        assert "card_events_acme" in captured

    def test_explicit_helper_rejects_mismatched_kind(self, stub_indexer):
        indexer, _ = stub_indexer
        env = EventEnvelope(
            event_id="evt_4",
            event_kind=EventKind.CAUSAL_CANDIDATE_PROPOSED,  # causal
            subject_rk="rk",
            produced_at=datetime.now(timezone.utc),
            provenance="test",
        )
        # Calling append_relation_event with a causal envelope must raise.
        with pytest.raises(ValueError, match="does not belong"):
            indexer.append_relation_event(
                tenant_id="acme", envelope=env, narrative="...",
            )

    def test_extra_payload_merges_with_envelope(self, stub_indexer):
        indexer, captured = stub_indexer
        env = EventEnvelope(
            event_id="evt_5", event_kind=EventKind.RELATION_TYPE_CANONICALIZED,
            subject_rk="p:X->Y",
            produced_at=datetime.now(timezone.utc),
            provenance="test",
            payload={"predicate": "p"},
        )
        indexer.append_event(
            tenant_id="acme", envelope=env, narrative="...",
            extra_payload={"surfaces_joined": "p,p2"},
        )
        store = captured["relation_events_acme"]
        meta = store.upserts[0]["metadata"]
        assert meta["predicate"] == "p"
        assert meta["surfaces_joined"] == "p,p2"


# ───────────────────────────────────────────────────────────────────────────
# Collection coverage — confirm every EventKind has a destination
# ───────────────────────────────────────────────────────────────────────────


class TestCollectionCoverage:
    def test_every_event_kind_has_a_destination(self):
        """Drift detector: any new EventKind must be added to
        `EventKind.for_collection()` mapping. If you add a kind without
        also adding it to the mapping, the indexer would raise at runtime;
        this test catches the omission at test time."""
        all_kinds = set(EventKind)
        routed = set()
        for spec_id in ("causal_events", "relation_events",
                        "protection_events", "card_events"):
            routed.update(EventKind.for_collection(spec_id))
        unrouted = all_kinds - routed
        assert not unrouted, (
            f"EventKinds with no destination collection: {sorted(k.value for k in unrouted)}. "
            "Add them to `EventKind.for_collection()` in vector/events.py."
        )

    def test_common_payload_keys_are_strings(self):
        # Sanity: the conventional payload key list must be importable + non-empty
        assert all(isinstance(k, str) for k in COMMON_PAYLOAD_KEYS)
        assert "predicate" in COMMON_PAYLOAD_KEYS
        assert "is_pii" in COMMON_PAYLOAD_KEYS

    def test_ensure_all_specs_in_registry(self):
        """All collection specs are registered in `all_collection_specs()`."""
        from ontology_store.vector import all_collection_specs
        ids = {s.tier_id for s in all_collection_specs()}
        for required in ("causal_events", "relation_events",
                         "protection_events", "card_events"):
            assert required in ids
