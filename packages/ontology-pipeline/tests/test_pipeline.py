"""Smoke + filter tests for the pipeline.

These tests do not require a live Postgres or an LLM. They:
  - Exercise the deterministic MDL build + filter logic against in-memory fixtures.
  - Verify content-hash idempotency: re-running with the same TableInfo yields 'unchanged'.
  - Verify a description-fill + annotation pass via a stub LLM provider.

Live integration against a real Postgres or OpenAI account is a separate
operator-run pass; see README.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel

from ontology_foundry.llm.provider import ModelProvider, ModelRole

from ontology_pipeline.annotate import SemanticVocab, enrich_annotations
from ontology_pipeline.config import (
    LLMConfig,
    OutputConfig,
    PipelineBehavior,
    PipelineConfig,
    PostgresConnection,
    SemanticLayerConfig,
    SourceConfig,
    TableFilter,
)
from ontology_pipeline.introspect import PostgresIntrospector
from ontology_pipeline.mdl import asset_rk, build_mdl, fill_descriptions
from ontology_pipeline.models import (
    ColumnInfo,
    GeneratedMDL,
    IntrospectionResult,
    TableInfo,
)
from ontology_pipeline.output import FilesystemSink
from ontology_pipeline.pipeline import _filter_tables
from ontology_pipeline.state import RunState, content_hash


# ───────────────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────────────

def _employee_table() -> TableInfo:
    return TableInfo(
        schema_name="public",
        name="csod_employee",
        description=None,
        primary_key=["EmployeeID"],
        is_view=False,
        view_definition=None,
        columns=[
            ColumnInfo(
                name="EmployeeID",
                sql_type="INTEGER",
                nullable=False,
                description="Unique employee identifier (PII).",
                is_primary_key=True,
            ),
            ColumnInfo(
                name="DepartmentID",
                sql_type="INTEGER",
                nullable=True,
                description="FK to department.",
                references_table="public.department",
                references_column="DepartmentID",
            ),
            ColumnInfo(
                name="ManagerID",
                sql_type="INTEGER",
                nullable=True,
                description=None,
            ),
            ColumnInfo(
                name="employment_status",
                sql_type="VARCHAR(20)",
                nullable=False,
                description="One of active / on_leave / terminated.",
            ),
        ],
    )


def _training_assignment_table() -> TableInfo:
    return TableInfo(
        schema_name="public",
        name="training_assignment",
        description="Per-employee training assignments.",
        primary_key=["assignment_id"],
        columns=[
            ColumnInfo(name="assignment_id", sql_type="INTEGER", nullable=False, is_primary_key=True),
            ColumnInfo(name="employee_id", sql_type="INTEGER", nullable=False),
            ColumnInfo(name="course_id", sql_type="INTEGER", nullable=False),
            ColumnInfo(name="due_date", sql_type="TIMESTAMP", nullable=True),
            ColumnInfo(name="completed_date", sql_type="TIMESTAMP", nullable=True),
        ],
    )


def _temp_audit_archive_table() -> TableInfo:
    return TableInfo(
        schema_name="public",
        name="tmp_audit_archive",
        columns=[ColumnInfo(name="id", sql_type="INTEGER", nullable=False)],
    )


def _make_introspection() -> IntrospectionResult:
    return IntrospectionResult(
        source_id="csod-servicenow-local",
        source_kind="postgres",
        catalog="serviceslearn3_prod",
        extracted_at=datetime.now(timezone.utc),
        tables=[_employee_table(), _training_assignment_table(), _temp_audit_archive_table()],
    )


# ───────────────────────────────────────────────────────────────────────────
# Stub LLM provider
# ───────────────────────────────────────────────────────────────────────────

class StubProvider(ModelProvider):
    """Deterministic LLM stub for tests. Returns canned JSON based on prompt sniffing."""

    def __init__(self) -> None:
        self.calls: list[tuple[ModelRole, str]] = []

    def complete(self, role: ModelRole, prompt: str, *,
                 response_format: type[BaseModel] | None = None) -> str:
        self.calls.append((role, prompt))
        if "ANNOTATE" in prompt.upper() or "object_type CARDS" in prompt:
            return json.dumps({
                "concepts": ["employee"],
                "key_areas": ["Workforce"],
                "causal_relations": ["overdue_risk"],
                "confidence": 0.85,
                "rationale": "Stub: row-per-employee structure with employment_status indicates Employee.",
            })
        # description fill response
        return json.dumps({
            "table_description": "Stub description: an employee record with identity + department + status.",
            "columns": [
                {"name": "ManagerID", "description": "Stub: the EmployeeID of this employee's manager."},
            ],
        })


# ───────────────────────────────────────────────────────────────────────────
# Tests
# ───────────────────────────────────────────────────────────────────────────

class TestDeterministicMDL:
    def test_build_mdl_preserves_native_descriptions_with_provenance(self) -> None:
        t = _employee_table()
        mdl = build_mdl(source_id="csod-servicenow-local", catalog="serviceslearn3_prod", table=t)
        assert mdl.mdl_version == "2.0"
        assert mdl.schema == "public"
        assert len(mdl.models) == 1
        m = mdl.models[0]
        assert m.name == "csod_employee"
        assert m.description is None  # source-side table comment was None
        # column with native description preserves it + provenance
        emp_id = next(c for c in m.columns if c.name == "EmployeeID")
        assert emp_id.properties.description == "Unique employee identifier (PII)."
        assert emp_id.properties.description_provenance == "extractor:postgres_information_schema"
        assert emp_id.properties.is_primary_key is True
        # column with no native description has provenance None
        mgr = next(c for c in m.columns if c.name == "ManagerID")
        assert mgr.properties.description is None
        assert mgr.properties.description_provenance is None
        # FK reference encoded
        dep = next(c for c in m.columns if c.name == "DepartmentID")
        assert dep.properties.references is not None
        assert "department" in dep.properties.references

    def test_asset_rk_shape(self) -> None:
        t = _employee_table()
        rk = asset_rk("csod-servicenow-local", "serviceslearn3_prod", t)
        assert rk == "postgres://csod-servicenow-local.serviceslearn3_prod/public/csod_employee"

    def test_fill_descriptions_no_provider_returns_unchanged(self) -> None:
        t = _employee_table()
        mdl = build_mdl(source_id="csod-servicenow-local", catalog="acme", table=t)
        out_mdl, native, filled, table_desc_gen = fill_descriptions(mdl, provider=None)
        assert out_mdl is mdl
        # 3 columns have native descriptions in fixture
        assert native == 3
        assert filled == 0
        assert table_desc_gen is False

    def test_fill_descriptions_with_stub_provider_fills_gaps_only(self) -> None:
        t = _employee_table()
        mdl = build_mdl(source_id="csod-servicenow-local", catalog="acme", table=t)
        provider = StubProvider()
        out_mdl, native, filled, table_desc_gen = fill_descriptions(mdl, provider=provider)
        assert table_desc_gen is True
        assert out_mdl.models[0].description_provenance == "llm_doc_gap_fill"
        # Existing descriptions must NOT be overwritten
        emp_id = next(c for c in out_mdl.models[0].columns if c.name == "EmployeeID")
        assert emp_id.properties.description == "Unique employee identifier (PII)."
        assert emp_id.properties.description_provenance == "extractor:postgres_information_schema"
        # ManagerID was missing; LLM should have filled it
        mgr = next(c for c in out_mdl.models[0].columns if c.name == "ManagerID")
        assert mgr.properties.description is not None
        assert mgr.properties.description_provenance == "llm_doc_gap_fill"
        assert filled == 1


class TestAnnotation:
    def test_enrich_annotations_with_vocab_uses_only_known_ids(self) -> None:
        t = _employee_table()
        mdl = build_mdl(source_id="csod-servicenow-local", catalog="acme", table=t)
        from ontology_pipeline.annotate import CardSummary, KeyAreaEntry
        vocab = SemanticVocab(
            object_types=[CardSummary(id="employee", kind="object_type", body_excerpt="Employee is a person.")],
            causal_nodes=[CardSummary(id="overdue_risk", kind="causal_node", body_excerpt="Risk of overdue training.")],
            key_areas=[KeyAreaEntry(id="Workforce", description="Employee composition.")],
        )
        provider = StubProvider()
        # Explicit llm_only mode for legacy-parity assertion. The new default
        # ('ner_then_llm') is exercised by tests in test_annotate_ner.py.
        anno = enrich_annotations(
            mdl, vocab=vocab, provider=provider, source_model="gpt-4o-mini-stub",
            concepts_source="llm_only",
        )
        assert anno is not None
        assert anno.concepts == ["employee"]
        assert anno.key_areas == ["Workforce"]
        assert anno.causal_relations == ["overdue_risk"]
        assert anno.confidence == 0.85
        assert anno.source == "llm_enrichment"
        # MDL model mirrors the annotations
        assert mdl.models[0].concepts == ["employee"]

    def test_enrich_annotations_skips_when_vocab_empty(self) -> None:
        t = _employee_table()
        mdl = build_mdl(source_id="csod-servicenow-local", catalog="acme", table=t)
        anno = enrich_annotations(mdl, vocab=SemanticVocab(), provider=StubProvider())
        assert anno is None

    def test_enrich_annotations_skips_when_no_provider_in_llm_only(self) -> None:
        # In strict llm_only mode, no provider → no annotation. (Hybrid mode
        # gracefully falls back to NER — that behaviour is covered in
        # test_annotate_ner.py::TestEnrichAnnotationsGracefulDegradation.)
        t = _employee_table()
        mdl = build_mdl(source_id="csod-servicenow-local", catalog="acme", table=t)
        from ontology_pipeline.annotate import CardSummary
        vocab = SemanticVocab(
            object_types=[CardSummary(id="employee", kind="object_type", body_excerpt="...")],
        )
        anno = enrich_annotations(
            mdl, vocab=vocab, provider=None, concepts_source="llm_only",
        )
        assert anno is None


class TestFilters:
    def test_no_filter_returns_all(self) -> None:
        tables = _make_introspection().tables
        filt = TableFilter()
        out = _filter_tables(tables, filt)
        assert len(out) == 3

    def test_include_list_narrows(self) -> None:
        tables = _make_introspection().tables
        filt = TableFilter(include=["csod_employee"])
        out = _filter_tables(tables, filt)
        assert [t.name for t in out] == ["csod_employee"]

    def test_exclude_pattern_drops(self) -> None:
        tables = _make_introspection().tables
        filt = TableFilter(exclude_patterns=["tmp_*"])
        out = _filter_tables(tables, filt)
        names = [t.name for t in out]
        assert "tmp_audit_archive" not in names
        assert "csod_employee" in names

    def test_include_pattern_keeps_matches(self) -> None:
        tables = _make_introspection().tables
        filt = TableFilter(include_patterns=["csod_*", "training_*"])
        out = _filter_tables(tables, filt)
        names = {t.name for t in out}
        assert names == {"csod_employee", "training_assignment"}

    def test_exclude_wins_over_include(self) -> None:
        tables = _make_introspection().tables
        filt = TableFilter(
            include_patterns=["csod_*", "tmp_*"],
            exclude_patterns=["tmp_*"],
        )
        out = _filter_tables(tables, filt)
        names = {t.name for t in out}
        assert "tmp_audit_archive" not in names
        assert "csod_employee" in names


class TestContentHash:
    def test_same_table_same_hash(self) -> None:
        a = _employee_table()
        b = _employee_table()
        assert content_hash(a) == content_hash(b)

    def test_column_added_changes_hash(self) -> None:
        a = _employee_table()
        b = _employee_table()
        b.columns.append(ColumnInfo(name="hire_date", sql_type="DATE", nullable=True))
        assert content_hash(a) != content_hash(b)

    def test_description_change_changes_hash(self) -> None:
        a = _employee_table()
        b = _employee_table()
        b.columns[0].description = "(edited)"
        assert content_hash(a) != content_hash(b)

    def test_row_count_change_does_not_affect_hash(self) -> None:
        a = _employee_table()
        b = _employee_table()
        b.row_count_estimate = 999_999
        assert content_hash(a) == content_hash(b)


class TestRunState:
    def test_record_and_lookup_round_trip(self, tmp_path: Path) -> None:
        rs = RunState(tmp_path)
        assert rs.lookup(source_id="s", schema="public", table="t") is None
        rs.record(
            source_id="s", schema="public", table="t",
            content_hash_value="abc123", outcome="created",
        )
        rs.flush()
        # Reload from disk
        rs2 = RunState(tmp_path)
        assert rs2.lookup(source_id="s", schema="public", table="t") == "abc123"

    def test_flush_is_atomic(self, tmp_path: Path) -> None:
        rs = RunState(tmp_path)
        rs.record(
            source_id="s", schema="public", table="t",
            content_hash_value="abc", outcome="created",
        )
        rs.flush()
        state_file = tmp_path / "run_state.json"
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["sources"]["s"]["public"]["t"]["content_hash"] == "abc"


class TestFilesystemSink:
    def test_write_mdl_and_annotations(self, tmp_path: Path) -> None:
        sink = FilesystemSink(OutputConfig(kind="filesystem", base_dir=tmp_path))
        t = _employee_table()
        mdl = build_mdl(source_id="s", catalog="c", table=t)
        path = sink.write_mdl(source_id="s", schema="public", table="csod_employee", mdl=mdl)
        assert path.exists()
        assert path.parent.parent.name == "s"
        loaded = json.loads(path.read_text())
        assert loaded["mdl_version"] == "2.0"
        assert loaded["models"][0]["name"] == "csod_employee"


class TestConfig:
    def test_pipeline_config_loads_minimal_yaml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PG_USER", "u")
        monkeypatch.setenv("PG_PASSWORD", "p")
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text("""
source:
  source_id: my-src
  org_id: my-org
  kind: postgres
  connection:
    host: localhost
    database: mydb
    user: ${PG_USER}
    password: ${PG_PASSWORD}
  schemas: [public]
output:
  kind: filesystem
  base_dir: out/
""", encoding="utf-8")
        cfg = PipelineConfig.load(cfg_path)
        assert cfg.source.source_id == "my-src"
        assert cfg.source.connection.user == "u"
        assert cfg.source.connection.password == "p"  # nosec
        assert not cfg.tables.is_configured()
        assert cfg.pipeline.fill_descriptions is True
        assert cfg.pipeline.annotate is True
        assert cfg.pipeline.re_enrich_unchanged is False

    def test_load_null_semantic_layer_section(self, tmp_path: Path) -> None:
        """YAML `semantic_layer:` with only comments parses as null; should not fail."""
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(
            """
source:
  source_id: my-src
  org_id: my-org
  kind: postgres
  connection:
    host: localhost
    database: mydb
    user: u
    password: p
  schemas: [public]
semantic_layer:
output:
  kind: filesystem
  base_dir: out/
""",
            encoding="utf-8",
        )
        cfg = PipelineConfig.load(cfg_path)
        assert cfg.semantic_layer.cards_dir is None
        assert cfg.semantic_layer.key_areas_vocab_path is None
