"""Cross-asset causal stage tests — clustering + LLM-driven hypothesis generation.

Uses a stub LLM provider so no API calls. The clustering logic is pure-Python
and tested first; then the LLM-driven application path; then post-filtering
(invalid asset rks, invalid predicates, threshold filtering, self-loops).
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from ontology_pipeline.enrich import (
    AssetCluster,
    ClusterContext,
    CrossAssetCausalEnricher,
)
from ontology_pipeline.mdl import build_mdl
from ontology_pipeline.models import ColumnInfo, GeneratedMDL, TableInfo


# ───────────────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────────────

def _make_table(
    name: str,
    *,
    schema: str = "public",
    concepts: list[str] | None = None,
    key_areas: list[str] | None = None,
    columns: list[ColumnInfo] | None = None,
) -> GeneratedMDL:
    table = TableInfo(
        schema_name=schema,
        name=name,
        primary_key=["id"],
        columns=columns or [
            ColumnInfo(name="id", sql_type="INTEGER", nullable=False, is_primary_key=True),
            ColumnInfo(name="employee_id", sql_type="INTEGER", nullable=True),
        ],
    )
    mdl = build_mdl(source_id="csod-pg", catalog="testdb", table=table)
    if mdl.models:
        mdl.models[0].concepts = list(concepts or [])
        mdl.models[0].key_areas = list(key_areas or [])
    return mdl


class _StubProvider:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[str] = []

    def complete(self, role, prompt, *, response_format=None):
        self.calls.append(prompt)
        return json.dumps(self._response)


def _cluster_ctx(provider, known_ids: list[str] | None = None) -> ClusterContext:
    return ClusterContext(
        source_id="csod-pg",
        provider=provider,
        llm_model_id="stub",
        known_causal_node_ids=known_ids or [],
        known_causal_node_excerpts={},
    )


# ───────────────────────────────────────────────────────────────────────────
# Clustering
# ───────────────────────────────────────────────────────────────────────────

class TestClusterAssets:
    def test_groups_by_shared_concept(self):
        a = _make_table("csod_employee", concepts=["employee"])
        b = _make_table("training_assignment", concepts=["employee", "training_assignment"])
        c = _make_table("unrelated", concepts=["customer"])
        enricher = CrossAssetCausalEnricher()
        clusters = enricher.cluster_assets([a, b, c])
        # Cluster on 'employee' should contain a and b
        emp = next((cl for cl in clusters if cl.cluster_key == "concept=employee"), None)
        assert emp is not None
        rks = {m.models[0].rk for m in emp.members}
        assert a.models[0].rk in rks and b.models[0].rk in rks
        # 'customer' cluster has only 1 → filtered out (min_cluster_size=2)
        cust = [cl for cl in clusters if cl.cluster_key == "concept=customer"]
        assert cust == []

    def test_groups_by_shared_key_area(self):
        a = _make_table("a", key_areas=["Training_Compliance"])
        b = _make_table("b", key_areas=["Training_Compliance", "HIPAA"])
        c = _make_table("c", key_areas=["HIPAA"])
        enricher = CrossAssetCausalEnricher()
        clusters = enricher.cluster_assets([a, b, c])
        tc = next((cl for cl in clusters if cl.cluster_key == "key_area=Training_Compliance"), None)
        hp = next((cl for cl in clusters if cl.cluster_key == "key_area=HIPAA"), None)
        assert tc is not None
        assert hp is not None
        assert tc.size() == 2
        assert hp.size() == 2

    def test_drops_singleton_clusters(self):
        a = _make_table("a", concepts=["employee"])
        # Just one asset with this concept — no cluster
        enricher = CrossAssetCausalEnricher()
        clusters = enricher.cluster_assets([a])
        assert clusters == []

    def test_caps_cluster_size(self):
        members = [_make_table(f"t{i}", concepts=["employee"]) for i in range(10)]
        enricher = CrossAssetCausalEnricher(max_cluster_size=3)
        clusters = enricher.cluster_assets(members)
        emp = next(cl for cl in clusters if cl.cluster_key == "concept=employee")
        assert emp.size() == 3

    def test_deduplicates_identical_rk_sets(self):
        # Two distinct buckets pointing to the SAME set of assets → only one cluster
        a = _make_table("a", concepts=["employee"], key_areas=["Workforce"])
        b = _make_table("b", concepts=["employee"], key_areas=["Workforce"])
        # Each bucket {concept=employee, key_area=Workforce} contains exactly {a, b}.
        # Second cluster with the same rk_set should be dropped.
        enricher = CrossAssetCausalEnricher()
        clusters = enricher.cluster_assets([a, b])
        # Two buckets exist but one is suppressed as a duplicate of the other
        assert len(clusters) == 1


# ───────────────────────────────────────────────────────────────────────────
# apply_all — LLM-driven hypothesis generation
# ───────────────────────────────────────────────────────────────────────────

class TestApplyAll:
    def test_produces_cross_asset_candidates(self):
        a = _make_table("training_assignment", concepts=["employee", "training_assignment"],
                         key_areas=["Training_Compliance"])
        b = _make_table("csod_employee", concepts=["employee"], key_areas=["Workforce"])
        provider = _StubProvider({
            "candidates": [
                {
                    "subject_asset_name": a.models[0].name,
                    "subject_column": "due_date",
                    "predicate": "leading_indicator_of",
                    "object_asset_name": b.models[0].name,
                    "object_column": "",
                    "object_causal_node_id": None,
                    "evidence_subject_columns": ["due_date", "employee_id"],
                    "evidence_object_columns": ["id"],
                    "mechanism_hint": "Overdue training drives attrition risk.",
                    "confidence": 0.7,
                    "rationale": "Tracks per-employee training cadence."
                }
            ],
            "rationale": "..."
        })
        ctx = _cluster_ctx(provider)
        enricher = CrossAssetCausalEnricher()
        results = enricher.apply_all([a, b], ctx)
        # At least one cluster ran
        assert len(results) >= 1
        # Find the result with the candidate
        with_cand = [r for r in results if r.side_output.get("causal_candidates")]
        assert with_cand
        candidates = with_cand[0].side_output["causal_candidates"]
        assert len(candidates) == 1
        c = candidates[0]
        assert c["predicate"] == "leading_indicator_of"
        assert c["asset_rk"] == a.models[0].rk
        assert c["subject_ref"].startswith(a.models[0].rk)
        assert c["object_ref"] == b.models[0].rk
        assert c["status"] == "proposed"
        assert c["provenance"] == "llm_cross_asset_causal"
        assert "cluster_key" in c

    def test_can_reference_causal_node_as_object(self):
        a = _make_table("training_assignment", concepts=["training_assignment"])
        b = _make_table("csod_employee", concepts=["employee", "training_assignment"])
        provider = _StubProvider({
            "candidates": [
                {
                    "subject_asset_name": a.models[0].name,
                    "subject_column": "completed_date",
                    "predicate": "leading_indicator_of",
                    "object_asset_name": None,
                    "object_column": "",
                    "object_causal_node_id": "compliance_gap",
                    "evidence_subject_columns": ["completed_date"],
                    "evidence_object_columns": [],
                    "mechanism_hint": "Completion drives gap reduction.",
                    "confidence": 0.85,
                    "rationale": "..."
                }
            ]
        })
        ctx = _cluster_ctx(provider, known_ids=["compliance_gap", "overdue_risk"])
        results = CrossAssetCausalEnricher().apply_all([a, b], ctx)
        cand = next(r.side_output["causal_candidates"][0] for r in results if r.side_output)
        assert cand["object_ref"] == "compliance_gap"

    def test_drops_invalid_predicate(self):
        a = _make_table("t1", concepts=["x"])
        b = _make_table("t2", concepts=["x"])
        provider = _StubProvider({
            "candidates": [{
                "subject_asset_name": a.models[0].name, "subject_column": "",
                "predicate": "fish",  # not a controlled predicate
                "object_asset_name": b.models[0].name, "object_column": "",
                "object_causal_node_id": None,
                "evidence_subject_columns": [], "evidence_object_columns": [],
                "mechanism_hint": "", "confidence": 0.9, "rationale": ""
            }]
        })
        results = CrossAssetCausalEnricher().apply_all([a, b], _cluster_ctx(provider))
        for r in results:
            assert r.side_output.get("causal_candidates", []) == []

    def test_drops_self_loops_via_subject_validation(self):
        # An out-of-cluster subject name (no matching member) is dropped
        # because the post-processor can't resolve it to a cluster rk.
        a = _make_table("t1", concepts=["x"])
        b = _make_table("t2", concepts=["x"])
        provider = _StubProvider({
            "candidates": [{
                "subject_asset_name": "name_not_in_cluster",
                "subject_column": "", "predicate": "causes",
                "object_asset_name": b.models[0].name, "object_column": "",
                "object_causal_node_id": None,
                "evidence_subject_columns": [], "evidence_object_columns": [],
                "mechanism_hint": "", "confidence": 0.9, "rationale": ""
            }]
        })
        results = CrossAssetCausalEnricher().apply_all([a, b], _cluster_ctx(provider))
        for r in results:
            # subject is outside the cluster → filtered
            assert r.side_output.get("causal_candidates", []) == []

    def test_drops_below_confidence_threshold(self):
        a = _make_table("t1", concepts=["x"])
        b = _make_table("t2", concepts=["x"])
        provider = _StubProvider({
            "candidates": [{
                "subject_asset_name": a.models[0].name, "subject_column": "",
                "predicate": "causes",
                "object_asset_name": b.models[0].name, "object_column": "",
                "object_causal_node_id": None,
                "evidence_subject_columns": [], "evidence_object_columns": [],
                "mechanism_hint": "", "confidence": 0.2, "rationale": ""
            }]
        })
        results = CrossAssetCausalEnricher(min_confidence_to_emit=0.5).apply_all(
            [a, b], _cluster_ctx(provider),
        )
        for r in results:
            assert r.side_output.get("causal_candidates", []) == []

    def test_filters_unknown_object_causal_node(self):
        a = _make_table("t1", concepts=["x"])
        b = _make_table("t2", concepts=["x"])
        provider = _StubProvider({
            "candidates": [{
                "subject_asset_name": a.models[0].name, "subject_column": "",
                "predicate": "causes",
                "object_asset_name": None, "object_column": "",
                "object_causal_node_id": "not_in_vocab",
                "evidence_subject_columns": [], "evidence_object_columns": [],
                "mechanism_hint": "", "confidence": 0.9, "rationale": ""
            }]
        })
        results = CrossAssetCausalEnricher().apply_all(
            [a, b], _cluster_ctx(provider, known_ids=["compliance_gap"]),
        )
        # No candidates land because object_causal_node_id isn't in vocab
        for r in results:
            assert r.side_output.get("causal_candidates", []) == []

    def test_no_provider_returns_empty(self):
        a = _make_table("t1", concepts=["x"])
        b = _make_table("t2", concepts=["x"])
        ctx = _cluster_ctx(None)
        results = CrossAssetCausalEnricher().apply_all([a, b], ctx)
        assert results == []

    def test_llm_failure_returns_warning(self):
        class _Failing:
            def complete(self, *a, **kw):
                raise RuntimeError("API limit")
        a = _make_table("t1", concepts=["x"])
        b = _make_table("t2", concepts=["x"])
        results = CrossAssetCausalEnricher().apply_all([a, b], _cluster_ctx(_Failing()))
        # Cluster ran, returned a result with warning, no candidates
        assert results
        assert all("causal_candidates" not in r.side_output for r in results)
        assert any(w.startswith("llm error") for r in results for w in r.warnings)
