"""Vector-layer tests.

Filter-builder tests run without Qdrant (pure-Python logic). End-to-end
tests against a live Qdrant are gated on QDRANT_TEST_URL.

To run end-to-end:
    export QDRANT_TEST_URL=http://localhost:6333
    export OPENAI_API_KEY=sk-...
    pytest tests/test_vector.py
"""
from __future__ import annotations

import os
import uuid

import pytest


# ───────────────────────────────────────────────────────────────────────────
# Filter-builder unit tests (no Qdrant required, but importable)
# ───────────────────────────────────────────────────────────────────────────

try:
    from ontology_store.vector.store import build_qdrant_filter, to_qdrant_point_id
    _VECTOR_IMPORTABLE = True
except ImportError:
    _VECTOR_IMPORTABLE = False


pytestmark = pytest.mark.skipif(
    not _VECTOR_IMPORTABLE,
    reason="ontology-store[vector] extra not installed",
)


class TestFilterBuilder:
    def test_empty_returns_none(self) -> None:
        assert build_qdrant_filter(None) is None
        assert build_qdrant_filter({}) is None

    def test_simple_key_value(self) -> None:
        f = build_qdrant_filter({"project_id": "csod"})
        assert f is not None
        assert len(f.must) == 1
        c = f.must[0]
        assert c.key == "metadata.project_id"

    def test_eq_operator(self) -> None:
        f = build_qdrant_filter({"asset_kind": {"$eq": "table"}})
        assert f is not None
        assert f.must[0].key == "metadata.asset_kind"

    def test_in_operator(self) -> None:
        f = build_qdrant_filter({"lifecycle_stage": {"$in": ["production", "development"]}})
        assert f is not None
        # MatchAny — Qdrant model holds it under .match.any
        cond = f.must[0]
        assert cond.key == "metadata.lifecycle_stage"

    def test_list_value_treated_as_any(self) -> None:
        f = build_qdrant_filter({"concepts": ["employee", "training_assignment"]})
        assert f is not None
        assert f.must[0].key == "metadata.concepts"

    def test_and_of_subfilters(self) -> None:
        f = build_qdrant_filter({
            "$and": [
                {"project_id": "csod"},
                {"asset_kind": {"$eq": "table"}},
            ]
        })
        assert f is not None
        assert len(f.must) == 2

    def test_dollar_keys_not_treated_as_payload(self) -> None:
        # $or etc shouldn't generate FieldConditions
        f = build_qdrant_filter({"$or": [{"a": 1}], "asset_kind": "table"})
        assert f is not None
        # only the asset_kind condition lands; $or is not supported and is skipped
        assert len(f.must) == 1
        assert f.must[0].key == "metadata.asset_kind"

    def test_explicit_metadata_prefix_preserved(self) -> None:
        f = build_qdrant_filter({"metadata.org_id": "acme-corp"})
        assert f is not None
        assert f.must[0].key == "metadata.org_id"  # not double-prefixed


class TestPointId:
    def test_uuid_passthrough(self) -> None:
        u = str(uuid.uuid4())
        assert to_qdrant_point_id(u) == u

    def test_non_uuid_string_hashes_to_uuid5(self) -> None:
        out = to_qdrant_point_id("postgres://acme/public/csod_employee")
        assert out != "postgres://acme/public/csod_employee"
        # Should be a valid UUID
        uuid.UUID(out)

    def test_same_input_same_output(self) -> None:
        a = to_qdrant_point_id("rk-x")
        b = to_qdrant_point_id("rk-x")
        assert a == b

    def test_different_input_different_output(self) -> None:
        a = to_qdrant_point_id("rk-x")
        b = to_qdrant_point_id("rk-y")
        assert a != b


# ───────────────────────────────────────────────────────────────────────────
# Collection-name resolution
# ───────────────────────────────────────────────────────────────────────────

class TestCollectionNames:
    def test_env_scoped_resolution(self) -> None:
        from ontology_store.vector import HIER_T4_ASSETS, resolve_collection_name
        name = resolve_collection_name(HIER_T4_ASSETS, env="prod")
        assert name == "hier_t4_assets_prod"

    def test_tenant_scoped_resolution(self) -> None:
        from ontology_store.vector import CARDS, resolve_collection_name
        name = resolve_collection_name(CARDS, tenant_id="acme-corp")
        # Tenant id is slugified — '-' → '_', lowercased
        assert name == "cards_acme_corp"

    def test_env_scope_requires_env(self) -> None:
        from ontology_store.vector import HIER_T0_ORGS, resolve_collection_name
        with pytest.raises(ValueError):
            resolve_collection_name(HIER_T0_ORGS)

    def test_tenant_scope_requires_tenant_id(self) -> None:
        from ontology_store.vector import SQL_PAIRS, resolve_collection_name
        with pytest.raises(ValueError):
            resolve_collection_name(SQL_PAIRS)


# ───────────────────────────────────────────────────────────────────────────
# End-to-end against a live Qdrant (gated)
# ───────────────────────────────────────────────────────────────────────────

_LIVE = bool(os.environ.get("QDRANT_TEST_URL") and os.environ.get("OPENAI_API_KEY"))


@pytest.mark.skipif(not _LIVE, reason="Live Qdrant tests require QDRANT_TEST_URL + OPENAI_API_KEY")
class TestQdrantE2E:
    @pytest.fixture()
    def store(self):
        from ontology_store.vector import (
            OpenAIEmbedder,
            QdrantClientFactory,
            QdrantDocumentStore,
        )
        coll = f"test_ontology_store_{uuid.uuid4().hex[:8]}"
        client = QdrantClientFactory.get(url=os.environ["QDRANT_TEST_URL"])
        embedder = OpenAIEmbedder(model="text-embedding-3-small")
        store = QdrantDocumentStore(
            qdrant_client=client, collection_name=coll, embedder=embedder, batch_size=50,
        )
        store.ensure_collection()
        yield store
        # Cleanup
        store.drop()

    def test_upsert_and_search(self, store):
        store.upsert_points([
            {"id": "rk-a", "text": "Employee training assignment data",
             "metadata": {"asset_kind": "table", "concepts": ["employee", "training_assignment"],
                          "org_id": "acme-corp"}},
            {"id": "rk-b", "text": "Department compliance rollup",
             "metadata": {"asset_kind": "table", "concepts": ["department"], "org_id": "acme-corp"}},
        ])
        # Search with filter
        hits = store.search(query_text="training", where={"asset_kind": "table"}, k=5)
        assert len(hits) >= 1
        # Top hit should be related to training_assignment
        top = hits[0]
        assert top.score > 0

    def test_search_with_concept_filter(self, store):
        store.upsert_points([
            {"id": "rk-c", "text": "Customer accounts master",
             "metadata": {"concepts": ["customer"], "asset_kind": "table"}},
            {"id": "rk-d", "text": "Employee records",
             "metadata": {"concepts": ["employee"], "asset_kind": "table"}},
        ])
        hits = store.search(query_text="data", where={"concepts": ["employee"]}, k=5)
        # Should only return the employee-tagged record
        assert all(any(c == "employee" for c in (h.payload.get("metadata") or {}).get("concepts", [])) for h in hits)

    def test_count_and_delete_by_filter(self, store):
        store.upsert_points([
            {"id": f"rk-{i}", "text": f"point {i}",
             "metadata": {"batch": "X" if i % 2 == 0 else "Y"}}
            for i in range(10)
        ])
        assert store.count() >= 10
        n = store.delete_by_filter({"batch": "X"})
        assert n >= 5
        assert store.count({"batch": "X"}) == 0
