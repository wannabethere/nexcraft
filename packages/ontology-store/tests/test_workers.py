"""Reindex worker tests.

Filter and narrative-builder tests run without Postgres or Qdrant.
End-to-end worker tests require a live Postgres (gated on ONTOLOGY_STORE_TEST_URL).
The worker is exercised with a *stub indexer* that records calls — no Qdrant required.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import pytest

from ontology_store.schemas import TableContext, TableContextColumn
from ontology_store.workers.narrative import (
    build_asset_narrative,
    build_asset_payload,
)


# ───────────────────────────────────────────────────────────────────────────
# Narrative builders — pure-Python, no infra
# ───────────────────────────────────────────────────────────────────────────

def _employee_ctx() -> TableContext:
    return TableContext(
        asset_rk="postgres://acme-pg.testdb/public/csod_employee",
        asset_kind="table",
        source_id="acme-pg",
        catalog_uid="acme-pg::catalog::testdb",
        schema_rk="postgres://acme-pg.testdb/public",
        schema_name="public",
        name="csod_employee",
        description="Employee master record.",
        description_provenance="extractor:postgres_information_schema",
        concepts=["employee"],
        key_areas=["Workforce"],
        causal_relations=["overdue_risk"],
        lifecycle_stage="production",
        effective_sensitivity_class="confidential",
        primary_object_type="employee",
        columns=[
            TableContextColumn(
                name="employee_id",
                type="INTEGER",
                description="Unique employee identifier.",
                description_provenance="extractor:postgres_information_schema",
                is_primary_key=True,
            ),
            TableContextColumn(
                name="department_id",
                type="INTEGER",
                description="FK to department.",
                description_provenance="extractor:postgres_information_schema",
            ),
        ],
    )


class TestAssetNarrative:
    def test_includes_name_description_and_columns(self) -> None:
        text = build_asset_narrative(_employee_ctx())
        assert "csod_employee" in text
        assert "Employee master record." in text
        assert "PK:employee_id" in text
        assert "department_id" in text

    def test_includes_bound_card_excerpt_when_provided(self) -> None:
        text = build_asset_narrative(_employee_ctx(), bound_card_excerpt="An Employee is a person...")
        assert "Concept: An Employee is a person..." in text


class TestAssetPayload:
    def test_carries_payload_filters_from_table_context(self) -> None:
        p = build_asset_payload(_employee_ctx())
        assert p["asset_kind"] == "table"
        assert p["lifecycle_stage"] == "production"
        assert p["concepts"] == ["employee"]
        assert p["key_areas"] == ["Workforce"]
        assert p["causal_relations"] == ["overdue_risk"]
        assert p["source_id"] == "acme-pg"
        assert p["schema_rk"] == "postgres://acme-pg.testdb/public"
        assert p["primary_object_type"] == "employee"


# ───────────────────────────────────────────────────────────────────────────
# End-to-end: Postgres + queue + stub indexer
# ───────────────────────────────────────────────────────────────────────────

_LIVE_PG = bool(os.environ.get("ONTOLOGY_STORE_TEST_URL"))


@dataclass
class _StubIndexer:
    """Records calls so tests can verify what the worker tried to upsert."""
    upsert_asset_calls: list[dict] = field(default_factory=list)
    upsert_field_calls: list[dict] = field(default_factory=list)
    upsert_source_calls: list[dict] = field(default_factory=list)
    upsert_schema_calls: list[dict] = field(default_factory=list)
    upsert_card_calls: list[dict] = field(default_factory=list)

    def upsert_asset(self, asset_rk: str, text: str, payload: dict) -> None:
        self.upsert_asset_calls.append({"rk": asset_rk, "text": text, "payload": payload})

    def upsert_field(self, field_rk: str, text: str, payload: dict) -> None:
        self.upsert_field_calls.append({"rk": field_rk, "text": text, "payload": payload})

    def upsert_source(self, source_id: str, text: str, payload: dict) -> None:
        self.upsert_source_calls.append({"rk": source_id, "text": text, "payload": payload})

    def upsert_schema(self, schema_rk: str, text: str, payload: dict) -> None:
        self.upsert_schema_calls.append({"rk": schema_rk, "text": text, "payload": payload})

    def upsert_card(self, tenant_id: str, *, point_id: str, body: str, payload: dict) -> None:
        self.upsert_card_calls.append({
            "tenant_id": tenant_id, "point_id": point_id, "body": body, "payload": payload,
        })


@pytest.mark.skipif(not _LIVE_PG, reason="ONTOLOGY_STORE_TEST_URL not set; live Postgres tests skipped")
class TestReindexWorkerE2E:
    """Drives MDL writes → DAO auto-enqueue → worker dequeue → stub indexer."""

    @pytest.fixture()
    def db(self):
        from sqlalchemy import create_engine
        from ontology_store import Database
        from ontology_store.db.engine import Base
        from ontology_store.workers.queue import ReindexQueueRow  # noqa: F401 — registers table

        url = os.environ["ONTOLOGY_STORE_TEST_URL"]
        engine = create_engine(url, future=True)
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        return Database(engine)

    def test_mdl_write_enqueues_qdrant_asset_task(self, db) -> None:
        from ontology_store import (
            HierarchyDAO, MDLColumn, MDLColumnProperties, MDLDocument,
            MDLMaterialization, MDLModel, OrganizationIn, SourceIn,
        )
        from ontology_store.workers.queue import QueueDAO, TaskKind

        with db.session() as s:
            h = HierarchyDAO(s)
            h.upsert_organization(OrganizationIn(org_id="acme-corp", display_name="Acme"))
            h.upsert_source(SourceIn(
                source_id="acme-pg", org_id="acme-corp", kind="postgres",
                instance_name="Acme PG Test", display_name="Acme PG Test",
            ))
            mdl = MDLDocument(
                mdl_version="2.0", source_id="acme-pg", catalog="testdb", schema="public",
                models=[MDLModel(
                    name="csod_employee", rk="postgres://acme-pg.testdb/public/csod_employee",
                    is_view=False, tableReference={"table": "csod_employee"},
                    materialization=MDLMaterialization(kind="table", is_materialized=False),
                    columns=[MDLColumn(
                        name="employee_id", type="INTEGER", rk="postgres://acme-pg.testdb/public/csod_employee/employee_id",
                        properties=MDLColumnProperties(is_primary_key=True),
                    )],
                )],
            )
            h.upsert_mdl_document(mdl)

        # Queue should have exactly one pending qdrant_asset task
        with db.session() as s:
            dao = QueueDAO(s)
            assert dao.depth(status="pending", task_kind=TaskKind.QDRANT_ASSET.value) == 1

    def test_run_once_processes_queue_via_stub_indexer(self, db) -> None:
        from ontology_store import (
            HierarchyDAO, MDLColumn, MDLColumnProperties, MDLDocument,
            MDLMaterialization, MDLModel, OrganizationIn, SourceIn,
        )
        from ontology_store.workers import ReindexWorker
        from ontology_store.workers.queue import QueueDAO

        # Seed
        with db.session() as s:
            h = HierarchyDAO(s)
            h.upsert_organization(OrganizationIn(org_id="acme-corp", display_name="Acme"))
            h.upsert_source(SourceIn(
                source_id="acme-pg", org_id="acme-corp", kind="postgres",
                instance_name="Acme PG Test", display_name="Acme PG Test",
            ))
            mdl = MDLDocument(
                mdl_version="2.0", source_id="acme-pg", catalog="testdb", schema="public",
                models=[MDLModel(
                    name="csod_employee", rk="postgres://acme-pg.testdb/public/csod_employee",
                    is_view=False, tableReference={"table": "csod_employee"},
                    materialization=MDLMaterialization(kind="table", is_materialized=False),
                    columns=[MDLColumn(
                        name="employee_id", type="INTEGER", rk="postgres://acme-pg.testdb/public/csod_employee/employee_id",
                        properties=MDLColumnProperties(is_primary_key=True),
                    )],
                )],
            )
            h.upsert_mdl_document(mdl)

        indexer = _StubIndexer()
        worker = ReindexWorker(database=db, indexer=indexer)
        stats = worker.run_once()
        # One asset task + one field task (the MDL has one column).
        assert stats.processed == 2
        assert stats.succeeded == 2
        assert len(indexer.upsert_asset_calls) == 1
        call = indexer.upsert_asset_calls[0]
        assert call["rk"] == "postgres://acme-pg.testdb/public/csod_employee"
        # The payload must include the org_id resolved via Source
        assert call["payload"]["org_id"] == "acme-corp"
        # Field reindex landed too — single column → single point.
        assert len(indexer.upsert_field_calls) == 1
        fcall = indexer.upsert_field_calls[0]
        assert fcall["rk"] == "postgres://acme-pg.testdb/public/csod_employee/employee_id"
        assert fcall["payload"]["parent_rk"] == "postgres://acme-pg.testdb/public/csod_employee"
        assert fcall["payload"]["field_kind"] == "column"
        assert fcall["payload"]["org_id"] == "acme-corp"
        # is_primary_key=True on the MDL column → is_business_key=True downstream
        assert fcall["payload"]["is_business_key"] is True

        # Queue should be empty (both tasks marked done)
        with db.session() as s:
            assert QueueDAO(s).depth(status="pending") == 0

    def test_mdl_write_enqueues_qdrant_field_task_per_column(self, db) -> None:
        """Every column upserted via MDL triggers a qdrant_field reindex task."""
        from ontology_store import (
            HierarchyDAO, MDLColumn, MDLColumnProperties, MDLDocument,
            MDLMaterialization, MDLModel, OrganizationIn, SourceIn,
        )
        from ontology_store.workers.queue import QueueDAO, TaskKind

        with db.session() as s:
            h = HierarchyDAO(s)
            h.upsert_organization(OrganizationIn(org_id="acme-corp", display_name="Acme"))
            h.upsert_source(SourceIn(
                source_id="acme-pg", org_id="acme-corp", kind="postgres",
                instance_name="Acme PG Test", display_name="Acme PG Test",
            ))
            mdl = MDLDocument(
                mdl_version="2.0", source_id="acme-pg", catalog="testdb", schema="public",
                models=[MDLModel(
                    name="csod_employee", rk="postgres://acme-pg.testdb/public/csod_employee",
                    is_view=False, tableReference={"table": "csod_employee"},
                    materialization=MDLMaterialization(kind="table", is_materialized=False),
                    columns=[
                        MDLColumn(
                            name="employee_id", type="INTEGER",
                            rk="postgres://acme-pg.testdb/public/csod_employee/employee_id",
                            properties=MDLColumnProperties(is_primary_key=True),
                        ),
                        MDLColumn(
                            name="department", type="TEXT",
                            rk="postgres://acme-pg.testdb/public/csod_employee/department",
                            properties=MDLColumnProperties(),
                        ),
                    ],
                )],
            )
            h.upsert_mdl_document(mdl)

        with db.session() as s:
            dao = QueueDAO(s)
            assert dao.depth(status="pending", task_kind=TaskKind.QDRANT_ASSET.value) == 1
            assert dao.depth(status="pending", task_kind=TaskKind.QDRANT_FIELD.value) == 2

    def test_unknown_task_kind_is_skipped_not_blocking(self, db) -> None:
        from ontology_store.workers import ReindexWorker
        from ontology_store.workers.queue import QueueDAO

        with db.session() as s:
            QueueDAO(s).enqueue(task_kind="not_a_real_kind", payload={"x": 1})

        indexer = _StubIndexer()
        worker = ReindexWorker(database=db, indexer=indexer)
        stats = worker.run_once()
        # Unknown kinds are dispatched-and-marked-done so they don't clog the queue.
        assert stats.processed == 1
        assert stats.skipped == 1
        assert len(indexer.upsert_asset_calls) == 0
        with db.session() as s:
            assert QueueDAO(s).depth(status="pending") == 0

    def test_failure_retries_until_max_attempts(self, db) -> None:
        from ontology_store import (
            HierarchyDAO, MDLColumn, MDLColumnProperties, MDLDocument,
            MDLMaterialization, MDLModel, OrganizationIn, SourceIn,
        )
        from ontology_store.workers import ReindexWorker
        from ontology_store.workers.queue import QueueDAO, TaskStatus

        # Seed
        with db.session() as s:
            h = HierarchyDAO(s)
            h.upsert_organization(OrganizationIn(org_id="acme-corp", display_name="Acme"))
            h.upsert_source(SourceIn(
                source_id="acme-pg", org_id="acme-corp", kind="postgres",
                instance_name="Acme PG", display_name="Acme PG",
            ))
            mdl = MDLDocument(
                mdl_version="2.0", source_id="acme-pg", catalog="testdb", schema="public",
                models=[MDLModel(
                    name="t", rk="postgres://acme-pg.testdb/public/t",
                    is_view=False, tableReference={"table": "t"},
                    materialization=MDLMaterialization(kind="table", is_materialized=False),
                    columns=[],
                )],
            )
            h.upsert_mdl_document(mdl)

        class _FailingIndexer:
            def upsert_asset(self, *a, **kw):
                raise RuntimeError("boom")

        worker = ReindexWorker(
            database=db, indexer=_FailingIndexer(), max_attempts=2,
        )

        # First run: task attempts=1, marked pending (retry)
        worker.run_once()
        with db.session() as s:
            assert QueueDAO(s).depth(status=TaskStatus.PENDING.value) == 1

        # Second run: task attempts=2, hits max, marked failed
        worker.run_once()
        with db.session() as s:
            assert QueueDAO(s).depth(status=TaskStatus.FAILED.value) == 1
            assert QueueDAO(s).depth(status=TaskStatus.PENDING.value) == 0
