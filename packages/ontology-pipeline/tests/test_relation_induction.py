"""Tests for the foundry-backed relation-induction post-pass.

Layers covered:
  - `_run_relation_induction` drives `induce_schema` against synthetic
    accumulated relationships and persists the result via the sink.
  - Surface canonicalization: multiple surface predicates collapse to one
    canonical row.
  - min_support filter: predicates below the threshold are dropped.
  - Type-mismatch guard: an edge whose subject/object types don't match
    the canonical predicate's dominant domain/range is NOT attached.
  - FilesystemSink.write_relation_schema persists types + attachments.

DAO Postgres semantics live in `ontology-store/tests/test_relations.py`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ontology_pipeline.config import (
    LLMConfig,
    OutputConfig,
    PipelineBehavior,
    PipelineConfig,
    PostgresConnection,
    SourceConfig,
)
from ontology_pipeline.output import FilesystemSink


# ───────────────────────────────────────────────────────────────────────────
# Stubs
# ───────────────────────────────────────────────────────────────────────────


@dataclass
class _CanonicalizingProvider:
    """LLM stand-in that returns a canned canonicalization mapping.

    Mirrors `ontology_foundry.relations.artifacts.CanonicalizationResponse`
    shape: `{"clusters": [{"canonical": str, "members": [str, ...]}, ...]}`.
    Tests prime `cluster_spec` to control the mapping.
    """
    cluster_spec: dict[str, list[str]]
    calls: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        self.calls = []

    def complete(self, role, prompt, *, response_format=None):
        self.calls.append(prompt)
        clusters = [
            {"canonical": canonical, "members": members}
            for canonical, members in self.cluster_spec.items()
        ]
        return json.dumps({"clusters": clusters})


def _config(tmp_path: Path, *, min_support: int = 2) -> PipelineConfig:
    """Minimal config for driving _run_relation_induction."""
    return PipelineConfig(
        source=SourceConfig(
            source_id="csod-pg",
            org_id="acme",
            connection=PostgresConnection(
                host="localhost", port=5432, database="testdb",
                user="u", password="p",
            ),
        ),
        output=OutputConfig(kind="filesystem", base_dir=tmp_path),
        llm=LLMConfig(model="stub"),
        pipeline=PipelineBehavior(
            annotate=False,  # silence other LLM gates
            compute_column_stats=False,
            relation_induction_min_support=min_support,
            induce_relation_schema=True,
        ),
    )


def _rel(
    from_rk: str, to_rk: str, surface: str = "references",
    confidence: float = 0.8, edge_kind: str = "depends_on",
) -> dict[str, Any]:
    return {
        "from_rk": from_rk, "to_rk": to_rk,
        "edge_kind": edge_kind,
        "predicate_surface": surface,
        "confidence": confidence,
        "reason": None,
    }


# ───────────────────────────────────────────────────────────────────────────
# _run_relation_induction
# ───────────────────────────────────────────────────────────────────────────


class TestRunRelationInduction:
    def test_canonicalizes_surfaces_and_persists_tbox(self, tmp_path: Path):
        # Two surface predicates ("references", "fk_to") collapse to one canonical "references_entity".
        # Three edges → enough to clear min_support=2.
        rels = [
            _rel("rk:emp1", "rk:dept1", surface="references"),
            _rel("rk:emp2", "rk:dept1", surface="fk_to"),
            _rel("rk:emp3", "rk:dept1", surface="references"),
        ]
        concept_index = {
            "rk:emp1": "Employee", "rk:emp2": "Employee", "rk:emp3": "Employee",
            "rk:dept1": "Department",
        }
        provider = _CanonicalizingProvider({
            "references_entity": ["references", "fk_to"],
        })
        cfg = _config(tmp_path, min_support=2)
        sink = FilesystemSink(cfg.output)

        from ontology_pipeline.pipeline import _run_relation_induction
        llm_calls = _run_relation_induction(
            relationships=rels, concept_index=concept_index,
            config=cfg, provider=provider, sink=sink,
        )
        assert llm_calls == 1
        assert len(provider.calls) == 1  # one canonicalization

        out = tmp_path / "relation_schema" / "csod-pg" / "relation_schema.json"
        assert out.exists()
        data = json.loads(out.read_text())
        # One canonical predicate persisted
        assert len(data["types"]) == 1
        t = data["types"][0]
        assert t["predicate"] == "references_entity"
        assert t["domain"] == "Employee"
        assert t["range_type"] == "Department"
        assert t["evidence_count"] == 3
        assert set(t["surfaces"]) == {"references", "fk_to"}
        # All three edges attached
        attached = data["attachments"]
        assert len(attached) == 3
        assert all(a["predicate"] == "references_entity" for a in attached)

    def test_min_support_filters_out_low_support_predicates(self, tmp_path: Path):
        # Only one edge with surface="rare_predicate" — below default min_support=2.
        rels = [
            _rel("rk:a", "rk:b", surface="common_predicate"),
            _rel("rk:c", "rk:d", surface="common_predicate"),
            _rel("rk:e", "rk:f", surface="rare_predicate"),
        ]
        concepts = {"rk:a": "X", "rk:b": "Y", "rk:c": "X", "rk:d": "Y",
                    "rk:e": "X", "rk:f": "Y"}
        provider = _CanonicalizingProvider({
            "common_predicate": ["common_predicate"],
            "rare_predicate": ["rare_predicate"],
        })
        cfg = _config(tmp_path, min_support=2)
        sink = FilesystemSink(cfg.output)
        from ontology_pipeline.pipeline import _run_relation_induction
        _run_relation_induction(
            relationships=rels, concept_index=concepts,
            config=cfg, provider=provider, sink=sink,
        )
        out = tmp_path / "relation_schema" / "csod-pg" / "relation_schema.json"
        data = json.loads(out.read_text())
        # Only common_predicate survives
        preds = {t["predicate"] for t in data["types"]}
        assert preds == {"common_predicate"}
        # The rare edge gets no attachment
        attached = data["attachments"]
        assert {(a["from_rk"], a["to_rk"]) for a in attached} == {
            ("rk:a", "rk:b"), ("rk:c", "rk:d"),
        }

    def test_type_mismatch_edges_are_not_attached(self, tmp_path: Path):
        # `references` canonicalizes; dominant domain=Employee, range=Department.
        # One off-type edge (Course → Department) should NOT be attached even
        # though it shares the surface predicate.
        rels = [
            _rel("rk:emp1", "rk:dept1", surface="references"),
            _rel("rk:emp2", "rk:dept2", surface="references"),
            _rel("rk:emp3", "rk:dept3", surface="references"),
            _rel("rk:course1", "rk:dept4", surface="references"),
        ]
        concepts = {
            "rk:emp1": "Employee", "rk:emp2": "Employee", "rk:emp3": "Employee",
            "rk:dept1": "Department", "rk:dept2": "Department",
            "rk:dept3": "Department", "rk:dept4": "Department",
            "rk:course1": "Course",
        }
        provider = _CanonicalizingProvider({"references": ["references"]})
        cfg = _config(tmp_path, min_support=2)
        sink = FilesystemSink(cfg.output)
        from ontology_pipeline.pipeline import _run_relation_induction
        _run_relation_induction(
            relationships=rels, concept_index=concepts,
            config=cfg, provider=provider, sink=sink,
        )
        out = tmp_path / "relation_schema" / "csod-pg" / "relation_schema.json"
        data = json.loads(out.read_text())
        t = data["types"][0]
        assert t["domain"] == "Employee"
        assert t["range_type"] == "Department"
        # The Course → Department edge is NOT in attachments
        attached_pairs = {(a["from_rk"], a["to_rk"]) for a in data["attachments"]}
        assert ("rk:course1", "rk:dept4") not in attached_pairs
        assert len(attached_pairs) == 3

    def test_no_edges_returns_zero_and_writes_nothing(self, tmp_path: Path):
        cfg = _config(tmp_path)
        sink = FilesystemSink(cfg.output)
        provider = _CanonicalizingProvider({})
        from ontology_pipeline.pipeline import _run_relation_induction
        result = _run_relation_induction(
            relationships=[], concept_index={},
            config=cfg, provider=provider, sink=sink,
        )
        assert result == 0
        assert not (tmp_path / "relation_schema").exists()

    def test_unknown_concept_defaults_to_asset_type(self, tmp_path: Path):
        # When concept_index is missing entries, subject_type/object_type
        # fall back to "asset" — the run still completes.
        rels = [
            _rel("rk:a", "rk:b", surface="references"),
            _rel("rk:c", "rk:d", surface="references"),
        ]
        provider = _CanonicalizingProvider({"references": ["references"]})
        cfg = _config(tmp_path, min_support=2)
        sink = FilesystemSink(cfg.output)
        from ontology_pipeline.pipeline import _run_relation_induction
        _run_relation_induction(
            relationships=rels, concept_index={},  # empty
            config=cfg, provider=provider, sink=sink,
        )
        out = tmp_path / "relation_schema" / "csod-pg" / "relation_schema.json"
        data = json.loads(out.read_text())
        t = data["types"][0]
        assert t["domain"] == "asset"
        assert t["range_type"] == "asset"


# ───────────────────────────────────────────────────────────────────────────
# FilesystemSink.write_relation_schema
# ───────────────────────────────────────────────────────────────────────────


class TestFilesystemSinkRelationSchema:
    def test_writes_combined_types_and_attachments(self, tmp_path: Path):
        from ontology_store.dao import RelationTypeIn
        sink = FilesystemSink(OutputConfig(kind="filesystem", base_dir=tmp_path))
        types = [
            RelationTypeIn(
                predicate="references", domain="Employee", range_type="Department",
                confidence=0.85, evidence_count=3,
                surfaces=["references", "fk_to"], provenance="induce_schema",
            ),
        ]
        attachments = [
            {"from_rk": "rk:emp1", "to_rk": "rk:dept1",
             "edge_kind": "depends_on", "predicate": "references",
             "domain": "Employee", "range_type": "Department"},
        ]
        sink.write_relation_schema(
            source_id="csod-pg", types=types, attachments=attachments,
        )
        target = tmp_path / "relation_schema" / "csod-pg" / "relation_schema.json"
        assert target.exists()
        data = json.loads(target.read_text())
        assert len(data["types"]) == 1
        assert data["types"][0]["evidence_count"] == 3
        assert data["attachments"] == attachments

    def test_skips_when_both_empty(self, tmp_path: Path):
        sink = FilesystemSink(OutputConfig(kind="filesystem", base_dir=tmp_path))
        sink.write_relation_schema(source_id="x", types=[], attachments=[])
        assert not (tmp_path / "relation_schema").exists()
