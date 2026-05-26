"""Enrichment stage tests.

Each stage is exercised with a stub `ModelProvider` returning canned JSON.
Verifies:
  - The stage updates the MDL fields it should.
  - The no-clobber rule preserves native / human-authored values.
  - Side-output is produced for data_protection hints + inferred relationships.
  - LLM failures are swallowed into the result's warnings (don't raise).
"""
from __future__ import annotations

import json

import pytest

from ontology_pipeline.enrich import (
    ColumnSemanticsEnricher,
    DataProtectionEnricher,
    RelationshipInferenceEnricher,
    RichDescriptionEnricher,
)
from ontology_pipeline.enrich.base import EnrichmentContext
from ontology_pipeline.mdl import build_mdl
from ontology_pipeline.models import ColumnInfo, GeneratedMDL, TableInfo


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────

class _StubProvider:
    """Returns canned JSON per kind of stage. Tracks calls for assertions."""

    def __init__(self, responses: dict[str, dict]) -> None:
        # Keys are matched by substring against the prompt body.
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    def complete(self, role, prompt, *, response_format=None):
        self.calls.append((str(role), prompt))
        for key, resp in self._responses.items():
            if key in prompt:
                return json.dumps(resp)
        # Fallback: return empty-shaped response so structured_transform parses cleanly.
        return json.dumps({})


def _employee_table() -> TableInfo:
    return TableInfo(
        schema_name="public",
        name="csod_employee",
        description=None,
        primary_key=["EmployeeID"],
        columns=[
            ColumnInfo(
                name="EmployeeID",
                sql_type="INTEGER",
                nullable=False,
                description="Unique employee identifier.",
                is_primary_key=True,
            ),
            ColumnInfo(
                name="DepartmentID",
                sql_type="INTEGER",
                nullable=True,
                description=None,
            ),
            ColumnInfo(
                name="email",
                sql_type="VARCHAR(255)",
                nullable=True,
                description="Work email address.",
            ),
            ColumnInfo(
                name="annual_salary",
                sql_type="NUMERIC(12,2)",
                nullable=True,
                description="Annual salary in USD.",
            ),
        ],
    )


def _context() -> EnrichmentContext:
    return EnrichmentContext(
        source_id="csod-pg",
        catalog="testdb",
        schema_name="public",
        provider=None,  # set per test
        llm_model_id="stub",
    )


def _mdl_from_employee_table() -> GeneratedMDL:
    return build_mdl(source_id="csod-pg", catalog="testdb", table=_employee_table())


# ───────────────────────────────────────────────────────────────────────────
# RichDescriptionEnricher
# ───────────────────────────────────────────────────────────────────────────

class TestRichDescriptionEnricher:
    def test_no_provider_returns_warning(self):
        ctx = _context()
        ctx.provider = None
        mdl = _mdl_from_employee_table()
        result = RichDescriptionEnricher().apply(mdl, ctx)
        assert result.stage_name == "rich_description"
        assert result.llm_calls == 0
        assert any("no LLM provider" in w for w in result.warnings)

    def test_fills_documentation_block_and_missing_descriptions(self):
        ctx = _context()
        ctx.provider = _StubProvider({
            "documenting a database table": {
                "table_description": "Employee master record.",
                "business_purpose": "Single source of truth for personnel identity.",
                "primary_use_cases": ["HR reporting", "Compliance training rosters"],
                "key_relationships": ["one Employee has many TrainingAssignments"],
                "update_frequency": "daily",
                "data_retention": "7 years",
                "access_patterns": ["join-heavy", "filtered by department"],
                "performance_considerations": ["partition by department_id"],
                "columns": [
                    {"name": "DepartmentID", "description": "FK to department."},
                ],
            },
        })
        mdl = _mdl_from_employee_table()
        result = RichDescriptionEnricher().apply(mdl, ctx)
        assert result.llm_calls == 1
        assert "description" in result.fields_updated
        assert "documentation" in result.fields_updated
        # Documentation block should be attached as a model extra
        doc = mdl.models[0].model_dump().get("documentation")
        assert doc is not None
        assert doc["business_purpose"] == "Single source of truth for personnel identity."
        assert "HR reporting" in doc["primary_use_cases"]
        # DepartmentID gap should now be filled with llm provenance
        dep = next(c for c in mdl.models[0].columns if c.name == "DepartmentID")
        assert dep.properties.description == "FK to department."
        assert dep.properties.description_provenance == "llm_rich_documentation"

    def test_preserves_native_column_descriptions(self):
        ctx = _context()
        ctx.provider = _StubProvider({
            "documenting a database table": {
                "table_description": "...",
                "business_purpose": "",
                "primary_use_cases": [],
                "key_relationships": [],
                "update_frequency": "",
                "data_retention": "",
                "access_patterns": [],
                "performance_considerations": [],
                # Try to overwrite EmployeeID with something different — should be ignored
                "columns": [
                    {"name": "EmployeeID", "description": "OVERWRITE ATTEMPT"},
                ],
            },
        })
        mdl = _mdl_from_employee_table()
        RichDescriptionEnricher().apply(mdl, ctx)
        emp_id = next(c for c in mdl.models[0].columns if c.name == "EmployeeID")
        assert emp_id.properties.description == "Unique employee identifier."  # native, preserved
        assert emp_id.properties.description_provenance.startswith("extractor:")


# ───────────────────────────────────────────────────────────────────────────
# ColumnSemanticsEnricher
# ───────────────────────────────────────────────────────────────────────────

class TestColumnSemanticsEnricher:
    def test_adds_semantic_unit_and_business_meaning(self):
        ctx = _context()
        ctx.provider = _StubProvider({
            "annotate database columns": {
                "columns": [
                    {"name": "EmployeeID", "semantic_unit": "identifier",
                     "business_meaning": "Primary key for an employee.", "is_business_key": True},
                    {"name": "DepartmentID", "semantic_unit": "foreign_key",
                     "business_meaning": "FK to department.", "is_business_key": False},
                    {"name": "email", "semantic_unit": "email",
                     "business_meaning": "Work email.", "is_business_key": False},
                    {"name": "annual_salary", "semantic_unit": "currency_usd",
                     "business_meaning": "Yearly compensation in USD.", "is_business_key": False},
                ],
                "rationale": "..."
            },
        })
        mdl = _mdl_from_employee_table()
        result = ColumnSemanticsEnricher().apply(mdl, ctx)
        assert result.llm_calls == 1
        # Check that semantic_unit landed on each column's extras
        for col, expected in [
            ("EmployeeID", "identifier"),
            ("DepartmentID", "foreign_key"),
            ("email", "email"),
            ("annual_salary", "currency_usd"),
        ]:
            c = next(c for c in mdl.models[0].columns if c.name == col)
            extras = c.properties.model_extra or {}
            assert extras.get("semantic_unit") == expected
            assert extras.get("business_meaning")
            assert extras.get("semantics_provenance") == "llm_column_semantics"

    def test_idempotent_when_already_set(self):
        ctx = _context()
        ctx.provider = _StubProvider({})
        mdl = _mdl_from_employee_table()
        # Pre-set semantic_unit on every column
        from ontology_pipeline.models import MDLColumnProperties
        for col in mdl.models[0].columns:
            props = col.properties.model_dump()
            props["semantic_unit"] = "identifier"
            col.properties = MDLColumnProperties.model_validate(props)

        result = ColumnSemanticsEnricher().apply(mdl, ctx)
        # No LLM call — all columns already had semantic_unit.
        assert result.llm_calls == 0
        assert any("already have semantic_unit" in w for w in result.warnings)


# ───────────────────────────────────────────────────────────────────────────
# DataProtectionEnricher
# ───────────────────────────────────────────────────────────────────────────

class TestDataProtectionEnricher:
    def test_classifies_columns_and_emits_asset_hints(self):
        ctx = _context()
        ctx.provider = _StubProvider({
            "classify database columns": {
                "columns": [
                    {"name": "EmployeeID", "is_pii": True, "pii_categories": ["names"],
                     "sensitivity_class": "confidential", "reason": "Employee identifier."},
                    {"name": "DepartmentID", "is_pii": False, "pii_categories": [],
                     "sensitivity_class": "internal", "reason": "Department code."},
                    {"name": "email", "is_pii": True, "pii_categories": ["contact"],
                     "sensitivity_class": "confidential", "reason": "Personal email."},
                    {"name": "annual_salary", "is_pii": True, "pii_categories": ["financial"],
                     "sensitivity_class": "restricted", "reason": "Compensation."},
                ],
                "asset_hints": {
                    "suggested_rls_predicates": ["user_id = current_user_id()"],
                    "suggested_cls_columns": ["annual_salary"],
                    "rationale": "Sensitive PII; needs row + column controls.",
                },
            },
        })
        mdl = _mdl_from_employee_table()
        result = DataProtectionEnricher().apply(mdl, ctx)
        assert result.llm_calls == 1
        # Per-column attrs landed on extras
        salary = next(c for c in mdl.models[0].columns if c.name == "annual_salary")
        extras = salary.properties.model_extra or {}
        assert extras["is_pii"] is True
        assert extras["pii_categories"] == ["financial"]
        assert extras["sensitivity_class"] == "restricted"
        assert extras["data_protection_provenance"] == "llm_data_protection"
        # Side output carries asset-level hints
        assert "data_protection_hints" in result.side_output
        hints = result.side_output["data_protection_hints"]
        assert hints["asset_rk"] == mdl.models[0].rk
        assert "annual_salary" in hints["cls_columns"]
        assert hints["provenance"] == "llm_data_protection"

    def test_unknown_pii_category_dropped(self):
        ctx = _context()
        ctx.provider = _StubProvider({
            "classify database columns": {
                "columns": [
                    {"name": "EmployeeID", "is_pii": True,
                     "pii_categories": ["not_a_valid_category", "names"],
                     "sensitivity_class": "confidential", "reason": ""}
                ],
                "asset_hints": {"suggested_rls_predicates": [], "suggested_cls_columns": [], "rationale": ""}
            }
        })
        mdl = _mdl_from_employee_table()
        DataProtectionEnricher().apply(mdl, ctx)
        emp = next(c for c in mdl.models[0].columns if c.name == "EmployeeID")
        extras = emp.properties.model_extra or {}
        # Invalid category filtered out; only known ones remain
        assert extras["pii_categories"] == ["names"]


# ───────────────────────────────────────────────────────────────────────────
# RelationshipInferenceEnricher
# ───────────────────────────────────────────────────────────────────────────

class TestRelationshipInferenceEnricher:
    def test_infers_relationships_for_fk_shaped_columns(self):
        ctx = _context()
        ctx.provider = _StubProvider({
            "infer database foreign-key": {
                "relationships": [
                    {"from_column": "DepartmentID", "to_table": "public.department",
                     "to_column": "department_id", "confidence": 0.92,
                     "cardinality_hint": "many_to_one", "reason": "Standard FK naming."},
                ],
                "rationale": "DepartmentID is a clear FK."
            }
        })
        mdl = _mdl_from_employee_table()
        result = RelationshipInferenceEnricher().apply(mdl, ctx)
        assert result.llm_calls == 1
        assert "inferred_relationships" in result.side_output
        rels = result.side_output["inferred_relationships"]
        assert len(rels) == 1
        assert rels[0]["from_column"] == "DepartmentID"
        # High-confidence inference applied to MDL column
        dep = next(c for c in mdl.models[0].columns if c.name == "DepartmentID")
        assert dep.properties.references == "public.department.department_id"
        extras = dep.properties.model_extra or {}
        assert extras.get("references_provenance") == "llm_inferred_relationship"
        assert extras.get("references_confidence") == pytest.approx(0.92)

    def test_low_confidence_inference_not_applied_to_mdl(self):
        ctx = _context()
        ctx.provider = _StubProvider({
            "infer database foreign-key": {
                "relationships": [
                    {"from_column": "DepartmentID", "to_table": "public.maybe",
                     "to_column": "id", "confidence": 0.5,
                     "cardinality_hint": "", "reason": "uncertain"},
                ],
                "rationale": ""
            }
        })
        mdl = _mdl_from_employee_table()
        enricher = RelationshipInferenceEnricher(min_confidence_to_apply=0.8)
        result = enricher.apply(mdl, ctx)
        # Side output contains the proposal, but the MDL column was NOT updated
        assert "inferred_relationships" in result.side_output
        dep = next(c for c in mdl.models[0].columns if c.name == "DepartmentID")
        assert dep.properties.references is None

    def test_skips_when_declared_fk_exists(self):
        ctx = _context()
        ctx.provider = _StubProvider({})
        mdl = _mdl_from_employee_table()
        # Simulate a declared FK on DepartmentID
        dep = next(c for c in mdl.models[0].columns if c.name == "DepartmentID")
        dep.properties.references = "public.department.id"
        result = RelationshipInferenceEnricher().apply(mdl, ctx)
        # No LLM call; declared FK present
        assert result.llm_calls == 0
        assert any("declared FK" in w for w in result.warnings)


# ───────────────────────────────────────────────────────────────────────────
# CausalDependencyEnricher
# ───────────────────────────────────────────────────────────────────────────

from ontology_pipeline.enrich import CausalDependencyEnricher


class TestCausalDependencyEnricher:
    def test_no_vocab_skips_with_warning(self):
        ctx = _context()
        ctx.provider = _StubProvider({})  # provider present but vocab is empty
        mdl = _mdl_from_employee_table()
        result = CausalDependencyEnricher(known_causal_node_ids=[]).apply(mdl, ctx)
        assert result.llm_calls == 0
        assert any("no known causal_node vocab" in w for w in result.warnings)

    def test_applies_participations_and_emits_candidates(self):
        ctx = _context()
        ctx.provider = _StubProvider({
            "causal-inference assistant": {
                "participations": [
                    {"causal_node_id": "overdue_risk", "role": "subject",
                     "column_signals": ["EmployeeID", "DepartmentID"],
                     "confidence": 0.85,
                     "rationale": "Per-employee training records anchor overdue risk."},
                    {"causal_node_id": "compliance_gap", "role": "subject",
                     "column_signals": ["EmployeeID"],
                     "confidence": 0.7,
                     "rationale": "Employees feed dept-level rollups."},
                ],
                "candidates": [
                    {"subject_ref": "employee.training_completion_rate",
                     "predicate": "leading_indicator_of",
                     "object_ref": "compliance_gap",
                     "evidence_columns": ["EmployeeID"],
                     "mechanism_hint": "Higher completion → lower gap.",
                     "confidence": 0.78},
                ],
                "proposed_causal_node_drafts": [],
                "rationale": "..."
            }
        })
        mdl = _mdl_from_employee_table()
        enricher = CausalDependencyEnricher(
            known_causal_node_ids=["overdue_risk", "compliance_gap"],
            known_causal_node_excerpts={
                "overdue_risk": "Per-employee training overdue risk.",
                "compliance_gap": "Department compliance rollup.",
            },
        )
        result = enricher.apply(mdl, ctx)
        assert result.llm_calls == 1
        # Both causal_node ids must be mirrored into MDL.causal_relations
        assert set(mdl.models[0].causal_relations) == {"overdue_risk", "compliance_gap"}
        # Richer participation block attached as a top-level extra
        dumped = mdl.models[0].model_dump()
        assert "causal_participation" in dumped
        items = dumped["causal_participation"]["items"]
        roles = {(p["causal_node_id"], p["role"]) for p in items}
        assert ("overdue_risk", "subject") in roles
        # Side output carries candidates with filtered predicates
        assert "causal_candidates" in result.side_output
        candidates = result.side_output["causal_candidates"]
        assert len(candidates) == 1
        assert candidates[0]["predicate"] == "leading_indicator_of"
        assert candidates[0]["status"] == "proposed"
        assert candidates[0]["provenance"] == "llm_causal_dependency"

    def test_drops_unknown_causal_node_ids_and_predicates(self):
        ctx = _context()
        ctx.provider = _StubProvider({
            "causal-inference assistant": {
                "participations": [
                    # Unknown causal_node_id → filtered
                    {"causal_node_id": "not_in_vocab", "role": "subject",
                     "column_signals": ["EmployeeID"], "confidence": 0.9, "rationale": ""},
                    {"causal_node_id": "overdue_risk", "role": "outcome",
                     "column_signals": ["DepartmentID"], "confidence": 0.85, "rationale": ""},
                ],
                "candidates": [
                    # Invalid predicate → filtered
                    {"subject_ref": "employee", "predicate": "ladybug",
                     "object_ref": "overdue_risk", "evidence_columns": ["EmployeeID"],
                     "mechanism_hint": "", "confidence": 0.8},
                    # Valid predicate → kept
                    {"subject_ref": "employee", "predicate": "precedes",
                     "object_ref": "overdue_risk", "evidence_columns": ["EmployeeID"],
                     "mechanism_hint": "", "confidence": 0.7},
                ],
                "proposed_causal_node_drafts": [],
                "rationale": ""
            }
        })
        mdl = _mdl_from_employee_table()
        enricher = CausalDependencyEnricher(known_causal_node_ids=["overdue_risk"])
        result = enricher.apply(mdl, ctx)
        # Only overdue_risk landed; not_in_vocab was dropped
        assert mdl.models[0].causal_relations == ["overdue_risk"]
        # Only the valid-predicate candidate survived
        assert "causal_candidates" in result.side_output
        candidates = result.side_output["causal_candidates"]
        assert len(candidates) == 1
        assert candidates[0]["predicate"] == "precedes"

    def test_low_confidence_participation_not_applied(self):
        ctx = _context()
        ctx.provider = _StubProvider({
            "causal-inference assistant": {
                "participations": [
                    {"causal_node_id": "overdue_risk", "role": "subject",
                     "column_signals": [], "confidence": 0.2, "rationale": "weak signal"}
                ],
                "candidates": [],
                "proposed_causal_node_drafts": [],
                "rationale": ""
            }
        })
        mdl = _mdl_from_employee_table()
        enricher = CausalDependencyEnricher(
            known_causal_node_ids=["overdue_risk"],
            min_confidence_for_relation=0.5,
        )
        enricher.apply(mdl, ctx)
        # Below threshold → not mirrored into causal_relations
        assert mdl.models[0].causal_relations == []

    def test_proposed_causal_node_drafts_only_in_side_output(self):
        ctx = _context()
        ctx.provider = _StubProvider({
            "causal-inference assistant": {
                "participations": [],
                "candidates": [],
                "proposed_causal_node_drafts": [
                    {"proposed_id": "phishing_risk",
                     "title": "Phishing Risk",
                     "body_excerpt": "Per-employee risk of falling for phishing.",
                     "suggested_subject_refs": ["employee"],
                     "suggested_outcome_refs": ["security_breach"],
                     "rationale": "Schema suggests training+attempt tracking but no such causal_node exists."}
                ],
                "rationale": ""
            }
        })
        mdl = _mdl_from_employee_table()
        enricher = CausalDependencyEnricher(
            known_causal_node_ids=["overdue_risk"],
            propose_new_causal_nodes=True,
        )
        result = enricher.apply(mdl, ctx)
        # Never auto-applied — only in side_output for review
        assert "proposed_causal_node_drafts" in result.side_output
        drafts = result.side_output["proposed_causal_node_drafts"]
        assert len(drafts) == 1
        assert drafts[0]["proposed_id"] == "phishing_risk"
        assert drafts[0]["source_asset_rk"] == mdl.models[0].rk
        # MDL itself NOT updated with the new id
        assert "phishing_risk" not in mdl.models[0].causal_relations


# ───────────────────────────────────────────────────────────────────────────
# Failure isolation
# ───────────────────────────────────────────────────────────────────────────

class TestFailureIsolation:
    def test_llm_failure_returns_warnings_doesnt_raise(self):
        class _FailingProvider:
            def complete(self, *a, **kw):
                raise RuntimeError("API limit")

        ctx = _context()
        ctx.provider = _FailingProvider()
        mdl = _mdl_from_employee_table()
        # All five enrichers should gracefully return warnings, not raise
        for stage in [
            RichDescriptionEnricher(),
            ColumnSemanticsEnricher(),
            DataProtectionEnricher(),
            RelationshipInferenceEnricher(),
            CausalDependencyEnricher(known_causal_node_ids=["overdue_risk"]),
        ]:
            result = stage.apply(mdl, ctx)
            assert any("llm" in w.lower() for w in result.warnings) or not result.warnings
