"""Smoke tests for ontology-store DAOs.

These tests require Postgres because the models use Postgres-specific types
(ARRAY, JSONB, GIN indexes). When a CI / local Postgres isn't available, set
the env var ONTOLOGY_STORE_TEST_URL or skip the tests.

To run:
    export ONTOLOGY_STORE_TEST_URL=postgresql+psycopg://...@localhost/ontology_test
    pytest tests/

These tests create + drop the schema each run, so use a dedicated test DB.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine

from ontology_store import (
    AnnotationDAO,
    AssetAnnotations,
    AssetReader,
    CatalogIn,
    Database,
    HierarchyDAO,
    MDLColumn,
    MDLColumnProperties,
    MDLDocument,
    MDLMaterialization,
    MDLModel,
    OrganizationIn,
    RetrievalScope,
    SourceIn,
)
from ontology_store.db.engine import Base


pytestmark = pytest.mark.skipif(
    not os.environ.get("ONTOLOGY_STORE_TEST_URL"),
    reason="ONTOLOGY_STORE_TEST_URL must point at a clean Postgres test database",
)


@pytest.fixture(scope="function")
def db() -> Database:
    url = os.environ["ONTOLOGY_STORE_TEST_URL"]
    engine = create_engine(url, future=True)
    # Clean slate for each test
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return Database(engine)


def _make_mdl_doc(rk: str = "postgres://acme-pg.testdb/public/csod_employee") -> MDLDocument:
    return MDLDocument(
        mdl_version="2.0",
        source_id="acme-pg",
        catalog="testdb",
        schema="public",
        models=[
            MDLModel(
                name="csod_employee",
                rk=rk,
                description="Employee master.",
                description_provenance="extractor:postgres_information_schema",
                is_view=False,
                tableReference={"table": "csod_employee"},
                materialization=MDLMaterialization(kind="table", is_materialized=False),
                columns=[
                    MDLColumn(
                        name="employee_id",
                        type="INTEGER",
                        notNull=True,
                        rk=f"{rk}/employee_id",
                        properties=MDLColumnProperties(
                            displayName="Employee ID",
                            description="Unique identifier.",
                            description_provenance="extractor:postgres_information_schema",
                            is_primary_key=True,
                        ),
                    ),
                    MDLColumn(
                        name="department_id",
                        type="INTEGER",
                        rk=f"{rk}/department_id",
                        properties=MDLColumnProperties(
                            references="public.department.department_id",
                        ),
                    ),
                ],
            ),
        ],
    )


def _seed_org_source(db: Database) -> None:
    with db.session() as s:
        h = HierarchyDAO(s, actor="test")
        h.upsert_organization(OrganizationIn(org_id="acme-corp", display_name="Acme"))
        h.upsert_source(SourceIn(
            source_id="acme-pg",
            org_id="acme-corp",
            kind="postgres",
            instance_name="Acme PG Test",
            display_name="Acme PG Test",
        ))


class TestHierarchyDAO:
    def test_upsert_organization_creates_then_updates(self, db: Database) -> None:
        with db.session() as s:
            HierarchyDAO(s).upsert_organization(OrganizationIn(
                org_id="acme-corp", display_name="Acme",
            ))
        with db.session() as s:
            HierarchyDAO(s).upsert_organization(OrganizationIn(
                org_id="acme-corp", display_name="Acme Corporation",
            ))
        with db.session() as s:
            from ontology_store.db import Organization
            org = s.get(Organization, "acme-corp")
            assert org is not None
            assert org.display_name == "Acme Corporation"

    def test_upsert_mdl_creates_full_spine(self, db: Database) -> None:
        _seed_org_source(db)
        with db.session() as s:
            HierarchyDAO(s).upsert_mdl_document(_make_mdl_doc())
        # Now read back everything
        with db.session() as s:
            from ontology_store.db import (
                ClusterMetadata,
                ColumnMetadata,
                DatabaseMetadata,
                SchemaCatalog,
                SchemaMetadata,
                TableExt,
                TableMetadata,
            )
            assert s.get(DatabaseMetadata, "postgres") is not None
            assert s.query(ClusterMetadata).count() == 1
            assert s.query(SchemaMetadata).count() == 1
            assert s.query(TableMetadata).count() == 1
            assert s.query(ColumnMetadata).count() == 2
            assert s.query(SchemaCatalog).count() == 1
            tbl_ext = s.query(TableExt).one()
            assert tbl_ext.lifecycle_stage == "production"
            assert tbl_ext.concepts == []  # no annotations yet


class TestAnnotationDAO:
    def test_llm_then_human_then_llm_clobber_semantics(self, db: Database) -> None:
        _seed_org_source(db)
        with db.session() as s:
            HierarchyDAO(s).upsert_mdl_document(_make_mdl_doc())

        rk = "postgres://acme-pg.testdb/public/csod_employee"

        # 1. LLM applies → all three fields land
        with db.session() as s:
            outcomes = AnnotationDAO(s).write(AssetAnnotations(
                asset_rk=rk,
                concepts=["employee"],
                key_areas=["Workforce"],
                causal_relations=["overdue_risk"],
                source="llm_enrichment",
                confidence=0.8,
            ))
            assert outcomes == {
                "concepts": "applied",
                "key_areas": "applied",
                "causal_relations": "applied",
            }

        # 2. Human edits concepts → wins
        with db.session() as s:
            outcomes = AnnotationDAO(s).write(AssetAnnotations(
                asset_rk=rk,
                concepts=["employee", "external_contractor"],
                key_areas=[],
                causal_relations=[],
                source="human",
                written_by="jane.k@acme.com",
            ))
            assert outcomes["concepts"] == "applied"
            assert outcomes["key_areas"] == "noop_empty"

        # 3. LLM tries again → preserved for concepts, still allowed for empty key_areas
        with db.session() as s:
            outcomes = AnnotationDAO(s).write(AssetAnnotations(
                asset_rk=rk,
                concepts=["employee"],
                key_areas=["HR"],
                causal_relations=[],
                source="llm_enrichment",
                confidence=0.9,
            ))
            assert outcomes["concepts"] == "skipped_clobber"
            assert outcomes["key_areas"] == "applied"  # llm trust > llm trust for key_areas; both 1; still applied

        # Verify final state
        with db.session() as s:
            from ontology_store.db import TableExt
            ext = s.get(TableExt, rk)
            assert "external_contractor" in ext.concepts


class TestAssetReader:
    def test_get_asset_returns_hydrated_context(self, db: Database) -> None:
        _seed_org_source(db)
        with db.session() as s:
            HierarchyDAO(s).upsert_mdl_document(_make_mdl_doc())
            AnnotationDAO(s).write(AssetAnnotations(
                asset_rk="postgres://acme-pg.testdb/public/csod_employee",
                concepts=["employee"],
                key_areas=["Workforce"],
                source="llm_enrichment",
                confidence=0.8,
            ))

        with db.session() as s:
            ctx = AssetReader(s).get_asset(
                "postgres://acme-pg.testdb/public/csod_employee"
            )
        assert ctx is not None
        assert ctx.name == "csod_employee"
        assert ctx.asset_kind == "table"
        assert ctx.concepts == ["employee"]
        assert ctx.key_areas == ["Workforce"]
        assert ctx.primary_object_type == "employee"
        assert any(c.name == "employee_id" and c.is_primary_key for c in ctx.columns)

    def test_list_by_concept(self, db: Database) -> None:
        _seed_org_source(db)
        # Two tables, one with the matching concept
        doc1 = _make_mdl_doc("postgres://acme-pg.testdb/public/csod_employee")
        doc2 = _make_mdl_doc("postgres://acme-pg.testdb/public/department")
        doc2.models[0].name = "department"
        doc2.models[0].tableReference = {"table": "department"}

        with db.session() as s:
            h = HierarchyDAO(s)
            h.upsert_mdl_document(doc1)
            h.upsert_mdl_document(doc2)
            AnnotationDAO(s).write(AssetAnnotations(
                asset_rk="postgres://acme-pg.testdb/public/csod_employee",
                concepts=["employee"], source="llm_enrichment",
            ))

        with db.session() as s:
            hits = AssetReader(s).list_assets(scope=RetrievalScope(
                org_id="acme-corp",
                concepts=["employee"],
            ))
        assert len(hits) == 1
        assert hits[0].name == "csod_employee"

    def test_search_ranks_by_concept_overlap(self, db: Database) -> None:
        _seed_org_source(db)
        # Two tables, one tagged with the searched concept
        doc1 = _make_mdl_doc("postgres://acme-pg.testdb/public/csod_employee")
        doc2 = _make_mdl_doc("postgres://acme-pg.testdb/public/csod_other")
        doc2.models[0].name = "csod_other"
        doc2.models[0].tableReference = {"table": "csod_other"}

        with db.session() as s:
            h = HierarchyDAO(s)
            h.upsert_mdl_document(doc1)
            h.upsert_mdl_document(doc2)
            AnnotationDAO(s).write(AssetAnnotations(
                asset_rk="postgres://acme-pg.testdb/public/csod_employee",
                concepts=["employee"], source="llm_enrichment",
            ))

        with db.session() as s:
            hits = AssetReader(s).search_assets(
                query="csod",
                scope=RetrievalScope(org_id="acme-corp", concepts=["employee"]),
                k=5,
            )
        # csod_employee should rank above csod_other due to concept overlap bonus
        assert hits[0].name == "csod_employee"
