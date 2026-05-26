"""Smoke tests for RelationTypeDAO + lineage_edge.predicate_id linkage.

Postgres-gated like the other DAO tests; skips locally without
ONTOLOGY_STORE_TEST_URL pointing at a clean test DB.

Run:
    export ONTOLOGY_STORE_TEST_URL=postgresql+psycopg://...@localhost/ontology_test
    pytest tests/test_relations.py
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine

from ontology_store import Database
from ontology_store.dao.relations import RelationTypeDAO, RelationTypeIn
from ontology_store.db import (
    ClusterMetadata,
    DatabaseMetadata,
    SchemaMetadata,
    TableMetadata,
)
from ontology_store.db.engine import Base
from ontology_store.db.models import (
    LineageEdge,
    Organization,
    Source,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("ONTOLOGY_STORE_TEST_URL"),
    reason="ONTOLOGY_STORE_TEST_URL must point at a clean Postgres test database",
)


@pytest.fixture(scope="function")
def db() -> Database:
    url = os.environ["ONTOLOGY_STORE_TEST_URL"]
    engine = create_engine(url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return Database(engine)


@pytest.fixture
def org(db: Database) -> str:
    with db.session() as s:
        s.add(Organization(org_id="acme", display_name="Acme"))
    return "acme"


def _seed_two_lineage_edges(db: Database) -> tuple[str, str, str]:
    """Insert two table rows + one lineage_edge between them. Returns (from_rk, to_rk, edge_kind)."""
    from_rk = "postgres://acme-pg/testdb/public/employee"
    to_rk = "postgres://acme-pg/testdb/public/department"
    edge_kind = "depends_on"
    with db.session() as s:
        s.add(Source(
            source_id="acme-pg", org_id="acme", kind="postgres",
            instance_name="acme-pg", display_name="acme-pg",
        ))
        s.add(ClusterMetadata(rk="postgres://acme-pg", name="acme-pg"))
        s.add(DatabaseMetadata(
            rk="postgres://acme-pg/testdb", name="testdb",
            cluster_rk="postgres://acme-pg",
        ))
        s.add(SchemaMetadata(
            rk="postgres://acme-pg/testdb/public", name="public",
            cluster_rk="postgres://acme-pg",
        ))
        s.add(TableMetadata(
            rk=from_rk, name="employee",
            schema_rk="postgres://acme-pg/testdb/public", is_view=False,
        ))
        s.add(TableMetadata(
            rk=to_rk, name="department",
            schema_rk="postgres://acme-pg/testdb/public", is_view=False,
        ))
        s.add(LineageEdge(
            from_rk=from_rk, from_kind="table",
            to_rk=to_rk, to_kind="table",
            edge_kind=edge_kind, evidence_kind="inferred_relationship",
            confidence=0.85, active=True,
        ))
    return from_rk, to_rk, edge_kind


# ───────────────────────────────────────────────────────────────────────────


class TestUpsertRelationType:
    def test_first_call_inserts(self, db: Database, org: str):
        spec = RelationTypeIn(
            predicate="references",
            domain="Employee", range_type="Department",
            confidence=0.9, evidence_count=5,
            surfaces=["references", "fk_to"],
        )
        with db.session() as s:
            row, outcome = RelationTypeDAO(s).upsert_relation_type(
                org_id=org, spec=spec,
            )
            assert outcome == "inserted"
            assert row.predicate == "references"
            assert row.evidence_count == 5
            assert row.surfaces == "fk_to,references"  # alphabetised

    def test_same_call_updates_in_place(self, db: Database, org: str):
        spec1 = RelationTypeIn(
            predicate="references", domain="Employee", range_type="Department",
            confidence=0.5, evidence_count=2, surfaces=["references"],
        )
        spec2 = RelationTypeIn(
            predicate="references", domain="Employee", range_type="Department",
            confidence=0.95, evidence_count=10,
            surfaces=["references", "links_to"],
        )
        with db.session() as s:
            RelationTypeDAO(s).upsert_relation_type(org_id=org, spec=spec1)
        with db.session() as s:
            _row, outcome = RelationTypeDAO(s).upsert_relation_type(org_id=org, spec=spec2)
            assert outcome == "updated"
        with db.session() as s:
            row = RelationTypeDAO(s).get_relation_type(
                org_id=org, predicate="references",
                domain="Employee", range_type="Department",
            )
            assert row.confidence == 0.95
            assert row.evidence_count == 10  # max of (2, 10)

    def test_evidence_count_never_decreases(self, db: Database, org: str):
        # A second induction run that saw fewer edges shouldn't downgrade
        # the observed evidence — we keep the max.
        spec_high = RelationTypeIn(
            predicate="references", domain="Employee", range_type="Department",
            evidence_count=10,
        )
        spec_low = RelationTypeIn(
            predicate="references", domain="Employee", range_type="Department",
            evidence_count=3,
        )
        with db.session() as s:
            RelationTypeDAO(s).upsert_relation_type(org_id=org, spec=spec_high)
        with db.session() as s:
            RelationTypeDAO(s).upsert_relation_type(org_id=org, spec=spec_low)
        with db.session() as s:
            row = RelationTypeDAO(s).get_relation_type(
                org_id=org, predicate="references",
                domain="Employee", range_type="Department",
            )
            assert row.evidence_count == 10

    def test_upsert_many_counts_correctly(self, db: Database, org: str):
        specs = [
            RelationTypeIn(predicate="references", domain="E", range_type="D",
                           evidence_count=3),
            RelationTypeIn(predicate="assigned_to", domain="E", range_type="T",
                           evidence_count=5),
        ]
        with db.session() as s:
            counts = RelationTypeDAO(s).upsert_many(org_id=org, specs=specs)
            assert counts == {"inserted": 2, "updated": 0}
        with db.session() as s:
            counts = RelationTypeDAO(s).upsert_many(org_id=org, specs=specs)
            assert counts == {"inserted": 0, "updated": 2}


class TestAttachPredicateToEdge:
    def test_attaches_predicate_to_existing_edge(self, db: Database, org: str):
        from_rk, to_rk, edge_kind = _seed_two_lineage_edges(db)
        spec = RelationTypeIn(
            predicate="references", domain="Employee", range_type="Department",
            evidence_count=1,
        )
        with db.session() as s:
            dao = RelationTypeDAO(s)
            row, _ = dao.upsert_relation_type(org_id=org, spec=spec)
            s.flush()
            n = dao.attach_predicate_to_edge(
                from_rk=from_rk, to_rk=to_rk, edge_kind=edge_kind,
                predicate_id=row.relation_type_pk,
            )
            assert n == 1
        with db.session() as s:
            from sqlalchemy import select
            edge = s.execute(
                select(LineageEdge).where(
                    LineageEdge.from_rk == from_rk,
                    LineageEdge.to_rk == to_rk,
                )
            ).scalar_one()
            assert edge.predicate_id is not None

    def test_idempotent_reattachment(self, db: Database, org: str):
        from_rk, to_rk, edge_kind = _seed_two_lineage_edges(db)
        spec = RelationTypeIn(
            predicate="references", domain="Employee", range_type="Department",
        )
        with db.session() as s:
            dao = RelationTypeDAO(s)
            row, _ = dao.upsert_relation_type(org_id=org, spec=spec)
            s.flush()
            n1 = dao.attach_predicate_to_edge(
                from_rk=from_rk, to_rk=to_rk, edge_kind=edge_kind,
                predicate_id=row.relation_type_pk,
            )
            n2 = dao.attach_predicate_to_edge(
                from_rk=from_rk, to_rk=to_rk, edge_kind=edge_kind,
                predicate_id=row.relation_type_pk,
            )
            assert n1 == 1
            assert n2 == 0  # second call is a no-op (already attached)

    def test_missing_edge_returns_zero(self, db: Database, org: str):
        spec = RelationTypeIn(
            predicate="references", domain="X", range_type="Y",
        )
        with db.session() as s:
            dao = RelationTypeDAO(s)
            row, _ = dao.upsert_relation_type(org_id=org, spec=spec)
            s.flush()
            n = dao.attach_predicate_to_edge(
                from_rk="postgres://nope", to_rk="postgres://nada",
                edge_kind="depends_on", predicate_id=row.relation_type_pk,
            )
            assert n == 0


class TestListRelationTypes:
    def test_filters_by_predicate(self, db: Database, org: str):
        specs = [
            RelationTypeIn(predicate="references", domain="E", range_type="D"),
            RelationTypeIn(predicate="references", domain="C", range_type="D"),
            RelationTypeIn(predicate="assigned_to", domain="E", range_type="T"),
        ]
        with db.session() as s:
            RelationTypeDAO(s).upsert_many(org_id=org, specs=specs)
        with db.session() as s:
            dao = RelationTypeDAO(s)
            refs = dao.list_relation_types(org_id=org, predicate="references")
            assert {(r.domain, r.range_type) for r in refs} == {("E", "D"), ("C", "D")}
            all_ = dao.list_relation_types(org_id=org)
            assert len(all_) == 3

    def test_filters_by_min_evidence_count(self, db: Database, org: str):
        specs = [
            RelationTypeIn(predicate="big", domain="E", range_type="D",
                           evidence_count=10),
            RelationTypeIn(predicate="small", domain="E", range_type="D",
                           evidence_count=1),
        ]
        with db.session() as s:
            RelationTypeDAO(s).upsert_many(org_id=org, specs=specs)
        with db.session() as s:
            rows = RelationTypeDAO(s).list_relation_types(
                org_id=org, min_evidence_count=5,
            )
            assert {r.predicate for r in rows} == {"big"}
