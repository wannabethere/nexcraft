"""Side-output routing tests.

Two layers:

  1. Stub-sink routing test — verifies the pipeline orchestrator calls the
     right sink methods with the right shape regardless of sink implementation.
     Runs without Postgres / Qdrant / LLM.

  2. FilesystemSink writer tests — confirms each side-output method writes
     a JSON file at the expected path and is a no-op on empty input.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ontology_pipeline.config import OutputConfig
from ontology_pipeline.models import AssetAnnotations, GeneratedMDL
from ontology_pipeline.output import FilesystemSink, TeeSink


# ───────────────────────────────────────────────────────────────────────────
# Stub sink — records calls
# ───────────────────────────────────────────────────────────────────────────

class _RecordingSink:
    """Implements the Sink protocol; records every call for assertions."""

    def __init__(self, *, base: Path):
        self._base = base
        self.write_mdl_calls: list[dict] = []
        self.write_annotations_calls: list[dict] = []
        self.write_inferred_relationships_calls: list[dict] = []
        self.write_data_protection_hints_calls: list[dict] = []
        self.write_causal_candidates_calls: list[dict] = []

    def base_dir(self) -> Path:
        return self._base

    def write_mdl(self, **kwargs):
        self.write_mdl_calls.append(kwargs)
        return self._base / "_recorded_mdl"

    def write_annotations(self, **kwargs):
        self.write_annotations_calls.append(kwargs)
        return self._base / "_recorded_anno"

    def write_inferred_relationships(self, **kwargs):
        self.write_inferred_relationships_calls.append(kwargs)

    def write_data_protection_hints(self, **kwargs):
        self.write_data_protection_hints_calls.append(kwargs)

    def write_causal_candidates(self, **kwargs):
        self.write_causal_candidates_calls.append(kwargs)


# ───────────────────────────────────────────────────────────────────────────
# Routing — pipeline._route_enrichment_side_output
# ───────────────────────────────────────────────────────────────────────────

class _FakeTable:
    def __init__(self, schema_name: str, name: str):
        self.schema_name = schema_name
        self.name = name

    @property
    def qualified_name(self) -> str:
        return f"{self.schema_name}.{self.name}"


class TestRouteEnrichmentSideOutput:
    def test_inferred_relationships_route_to_sink(self, tmp_path: Path):
        from ontology_pipeline.pipeline import _route_enrichment_side_output

        sink = _RecordingSink(base=tmp_path)
        table = _FakeTable("public", "csod_employee")
        side_output = {
            "inferred_relationships": [
                {
                    "from_table_rk": "postgres://acme-pg.testdb/public/csod_employee",
                    "from_column": "DepartmentID",
                    "to_table_qualified": "public.department",
                    "to_column": "id",
                    "confidence": 0.92,
                    "reason": "Standard FK shape.",
                    "cardinality_hint": "many_to_one",
                },
            ],
        }
        _route_enrichment_side_output(
            table=table,
            side_output=side_output,
            sink=sink,
            source_id="acme-pg",
            asset_rk="postgres://acme-pg.testdb/public/csod_employee",
        )
        assert len(sink.write_inferred_relationships_calls) == 1
        call = sink.write_inferred_relationships_calls[0]
        assert call["source_id"] == "acme-pg"
        assert call["schema"] == "public"
        assert call["table"] == "csod_employee"
        assert call["from_table_rk"] == "postgres://acme-pg.testdb/public/csod_employee"
        assert len(call["items"]) == 1
        assert call["items"][0]["confidence"] == 0.92

    def test_data_protection_hints_route_to_sink(self, tmp_path: Path):
        from ontology_pipeline.pipeline import _route_enrichment_side_output

        sink = _RecordingSink(base=tmp_path)
        table = _FakeTable("public", "csod_employee")
        side_output = {
            "data_protection_hints": {
                "asset_rk": "postgres://acme-pg.testdb/public/csod_employee",
                "rls_predicates": ["user_id = current_user_id()"],
                "cls_columns": ["annual_salary"],
                "rationale": "Sensitive PII",
                "provenance": "llm_data_protection",
            }
        }
        _route_enrichment_side_output(
            table=table, side_output=side_output, sink=sink,
            source_id="acme-pg",
            asset_rk="postgres://acme-pg.testdb/public/csod_employee",
        )
        assert len(sink.write_data_protection_hints_calls) == 1
        call = sink.write_data_protection_hints_calls[0]
        assert call["asset_rk"] == "postgres://acme-pg.testdb/public/csod_employee"
        assert call["hints"]["cls_columns"] == ["annual_salary"]

    def test_causal_candidates_route_to_sink(self, tmp_path: Path):
        from ontology_pipeline.pipeline import _route_enrichment_side_output

        sink = _RecordingSink(base=tmp_path)
        table = _FakeTable("public", "csod_employee")
        side_output = {
            "causal_candidates": [
                {
                    "asset_rk": "postgres://acme-pg.testdb/public/csod_employee",
                    "subject_ref": "employee.training_completion_rate",
                    "predicate": "leading_indicator_of",
                    "object_ref": "compliance_gap",
                    "evidence_columns": ["EmployeeID"],
                    "mechanism_hint": "Higher completion → lower gap.",
                    "confidence": 0.78,
                    "status": "proposed",
                    "provenance": "llm_causal_dependency",
                }
            ]
        }
        _route_enrichment_side_output(
            table=table, side_output=side_output, sink=sink,
            source_id="acme-pg",
            asset_rk="postgres://acme-pg.testdb/public/csod_employee",
        )
        assert len(sink.write_causal_candidates_calls) == 1
        call = sink.write_causal_candidates_calls[0]
        assert call["candidates"][0]["predicate"] == "leading_indicator_of"

    def test_empty_lists_skipped(self, tmp_path: Path):
        from ontology_pipeline.pipeline import _route_enrichment_side_output

        sink = _RecordingSink(base=tmp_path)
        table = _FakeTable("public", "t")
        _route_enrichment_side_output(
            table=table,
            side_output={
                "inferred_relationships": [],
                "causal_candidates": [],
            },
            sink=sink, source_id="s",
            asset_rk="rk",
        )
        # No calls when lists are empty
        assert sink.write_inferred_relationships_calls == []
        assert sink.write_causal_candidates_calls == []

    def test_sink_exception_is_caught_per_route(self, tmp_path: Path):
        from ontology_pipeline.pipeline import _route_enrichment_side_output

        class _PartiallyBrokenSink(_RecordingSink):
            def write_inferred_relationships(self, **kwargs):
                raise RuntimeError("DB down")

        sink = _PartiallyBrokenSink(base=tmp_path)
        table = _FakeTable("public", "t")
        # Mix: one route raises, one succeeds — both should be attempted
        _route_enrichment_side_output(
            table=table,
            side_output={
                "inferred_relationships": [{"from_table_rk": "x", "to_table_qualified": "y.z",
                                             "from_column": "a", "to_column": "b",
                                             "confidence": 0.9}],
                "causal_candidates": [{"asset_rk": "x", "subject_ref": "a",
                                       "predicate": "causes", "object_ref": "b",
                                       "confidence": 0.5}],
            },
            sink=sink, source_id="s", asset_rk="x",
        )
        # Causal candidates still landed even though inferred-relationships raised
        assert len(sink.write_causal_candidates_calls) == 1


# ───────────────────────────────────────────────────────────────────────────
# FilesystemSink writes
# ───────────────────────────────────────────────────────────────────────────

class TestFilesystemSinkSideOutputs:
    def test_inferred_relationships_writes_json(self, tmp_path: Path):
        sink = FilesystemSink(OutputConfig(kind="filesystem", base_dir=tmp_path))
        sink.write_inferred_relationships(
            source_id="acme-pg", schema="public", table="csod_employee",
            from_table_rk="postgres://acme-pg.testdb/public/csod_employee",
            items=[
                {"from_table_rk": "postgres://acme-pg.testdb/public/csod_employee",
                 "from_column": "DepartmentID", "to_table_qualified": "public.department",
                 "to_column": "id", "confidence": 0.92, "reason": "..."},
            ],
        )
        target = tmp_path / "inferred_relationships" / "acme-pg" / "public" / "csod_employee.json"
        assert target.exists()
        data = json.loads(target.read_text())
        assert data["from_table_rk"] == "postgres://acme-pg.testdb/public/csod_employee"
        assert len(data["items"]) == 1

    def test_data_protection_hints_writes_json(self, tmp_path: Path):
        sink = FilesystemSink(OutputConfig(kind="filesystem", base_dir=tmp_path))
        sink.write_data_protection_hints(
            source_id="acme-pg", schema="public", table="csod_employee",
            asset_rk="postgres://acme-pg.testdb/public/csod_employee",
            hints={"rls_predicates": ["x"], "cls_columns": ["y"],
                   "rationale": "z", "provenance": "llm"},
        )
        target = tmp_path / "data_protection_hints" / "acme-pg" / "public" / "csod_employee.json"
        assert target.exists()
        data = json.loads(target.read_text())
        assert data["cls_columns"] == ["y"]

    def test_causal_candidates_writes_json(self, tmp_path: Path):
        sink = FilesystemSink(OutputConfig(kind="filesystem", base_dir=tmp_path))
        sink.write_causal_candidates(
            source_id="acme-pg", schema="public", table="csod_employee",
            candidates=[{
                "asset_rk": "x", "subject_ref": "a", "predicate": "causes",
                "object_ref": "b", "confidence": 0.5,
            }],
        )
        target = tmp_path / "causal_candidates" / "acme-pg" / "public" / "csod_employee.json"
        assert target.exists()
        data = json.loads(target.read_text())
        assert len(data["candidates"]) == 1

    def test_empty_side_output_no_file(self, tmp_path: Path):
        sink = FilesystemSink(OutputConfig(kind="filesystem", base_dir=tmp_path))
        sink.write_inferred_relationships(
            source_id="s", schema="p", table="t",
            from_table_rk="rk", items=[],
        )
        sink.write_data_protection_hints(
            source_id="s", schema="p", table="t",
            asset_rk="rk", hints={},
        )
        sink.write_causal_candidates(
            source_id="s", schema="p", table="t", candidates=[],
        )
        # None of the directories should exist
        assert not (tmp_path / "inferred_relationships").exists()
        assert not (tmp_path / "data_protection_hints").exists()
        assert not (tmp_path / "causal_candidates").exists()


# ───────────────────────────────────────────────────────────────────────────
# TeeSink fan-out
# ───────────────────────────────────────────────────────────────────────────

class TestTeeSinkSideOutputs:
    def test_fans_out_to_every_sink(self, tmp_path: Path):
        sink_a = _RecordingSink(base=tmp_path / "a")
        sink_b = _RecordingSink(base=tmp_path / "b")
        tee = TeeSink([sink_a, sink_b])
        tee.write_inferred_relationships(
            source_id="s", schema="p", table="t",
            from_table_rk="rk", items=[{"to_table_qualified": "p.x"}],
        )
        assert len(sink_a.write_inferred_relationships_calls) == 1
        assert len(sink_b.write_inferred_relationships_calls) == 1
        tee.write_causal_candidates(
            source_id="s", schema="p", table="t",
            candidates=[{"asset_rk": "x", "subject_ref": "s",
                         "predicate": "causes", "object_ref": "o", "confidence": 0.5}],
        )
        assert len(sink_a.write_causal_candidates_calls) == 1
        assert len(sink_b.write_causal_candidates_calls) == 1


# ───────────────────────────────────────────────────────────────────────────
# HierarchyStoreSink — end-to-end (live Postgres gated)
# ───────────────────────────────────────────────────────────────────────────

import os

_LIVE_PG = bool(os.environ.get("ONTOLOGY_STORE_TEST_URL"))


@pytest.mark.skipif(not _LIVE_PG, reason="ONTOLOGY_STORE_TEST_URL not set")
class TestHierarchyStoreSinkSideOutputsE2E:
    @pytest.fixture()
    def db(self):
        from sqlalchemy import create_engine
        from ontology_store import Database
        from ontology_store.db.engine import Base
        # Pull all model modules so create_all sees them
        from ontology_store.db import eval_models  # noqa: F401
        from ontology_store.db import inference_models  # noqa: F401
        from ontology_store.workers.queue import ReindexQueueRow  # noqa: F401

        engine = create_engine(os.environ["ONTOLOGY_STORE_TEST_URL"], future=True)
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        return Database(engine)

    def _seed_source(self, db, source_id="acme-pg", org_id="acme-corp"):
        from ontology_store import (
            HierarchyDAO, OrganizationIn, SourceIn,
        )
        with db.session() as s:
            h = HierarchyDAO(s)
            h.upsert_organization(OrganizationIn(org_id=org_id, display_name=org_id))
            h.upsert_source(SourceIn(
                source_id=source_id, org_id=org_id, kind="postgres",
                instance_name=source_id, display_name=source_id,
            ))

    def test_inferred_relationships_become_lineage_edges(self, db):
        from ontology_pipeline.output import HierarchyStoreSink
        from ontology_store.db import LineageEdge

        self._seed_source(db)
        sink = HierarchyStoreSink(db=db, org_id="acme-corp")
        sink.write_inferred_relationships(
            source_id="acme-pg", schema="public", table="csod_employee",
            from_table_rk="postgres://acme-pg.testdb/public/csod_employee",
            items=[
                {"from_table_rk": "postgres://acme-pg.testdb/public/csod_employee",
                 "from_column": "DepartmentID",
                 "to_table_qualified": "public.department",
                 "to_column": "id",
                 "confidence": 0.92,
                 "reason": "Standard FK shape.",
                 "cardinality_hint": "many_to_one"},
            ],
        )
        with db.session() as s:
            edge = s.query(LineageEdge).filter_by(
                from_rk="postgres://acme-pg.testdb/public/csod_employee",
                to_rk="postgres://acme-pg.testdb/public/department",
            ).first()
            assert edge is not None
            assert edge.evidence_kind == "inferred_relationship"
            assert edge.confidence == pytest.approx(0.92)

    def test_causal_candidate_upsert_idempotent(self, db):
        from ontology_pipeline.output import HierarchyStoreSink
        from ontology_store.db import CausalCandidate

        self._seed_source(db)
        sink = HierarchyStoreSink(db=db, org_id="acme-corp")
        candidate = {
            "asset_rk": "postgres://acme-pg.testdb/public/csod_employee",
            "subject_ref": "employee.completion_rate",
            "predicate": "leading_indicator_of",
            "object_ref": "compliance_gap",
            "evidence_columns": ["EmployeeID"],
            "mechanism_hint": "Higher completion → lower gap.",
            "confidence": 0.78,
            "status": "proposed",
            "provenance": "llm_causal_dependency",
        }
        sink.write_causal_candidates(
            source_id="acme-pg", schema="public", table="csod_employee",
            candidates=[candidate],
        )
        sink.write_causal_candidates(
            source_id="acme-pg", schema="public", table="csod_employee",
            candidates=[{**candidate, "confidence": 0.85}],  # confidence improved
        )
        with db.session() as s:
            rows = s.query(CausalCandidate).filter_by(
                asset_rk=candidate["asset_rk"],
                subject_ref=candidate["subject_ref"],
                predicate=candidate["predicate"],
                object_ref=candidate["object_ref"],
            ).all()
            # Upserted to one row, not two
            assert len(rows) == 1
            assert rows[0].confidence == pytest.approx(0.85)

    def test_data_protection_hint_persists(self, db):
        from ontology_pipeline.output import HierarchyStoreSink
        from ontology_store.db import DataProtectionHint

        self._seed_source(db)
        sink = HierarchyStoreSink(db=db, org_id="acme-corp")
        sink.write_data_protection_hints(
            source_id="acme-pg", schema="public", table="csod_employee",
            asset_rk="postgres://acme-pg.testdb/public/csod_employee",
            hints={
                "rls_predicates": ["user_id = current_setting('app.user_id')::int"],
                "cls_columns": ["annual_salary"],
                "rationale": "Compensation is PII.",
                "provenance": "llm_data_protection",
            },
        )
        with db.session() as s:
            row = s.query(DataProtectionHint).filter_by(
                asset_rk="postgres://acme-pg.testdb/public/csod_employee",
            ).first()
            assert row is not None
            assert "annual_salary" in row.cls_columns
            assert row.status == "proposed"
