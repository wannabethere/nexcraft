"""Tests for the foundry-backed NER pre-pass + hybrid annotation modes.

Layers covered:
  - `build_lexicon` — surface enumeration + conflict handling
  - `propose_concept_candidates` / `propose_key_area_candidates` /
    `propose_causal_node_candidates` — end-to-end linker integration
  - `enrich_annotations` modes:
      * 'ner_only'      — deterministic, no LLM
      * 'ner_then_llm'  — LLM call receives NER grounding block in prompt
      * 'llm_only'      — legacy unchanged behaviour
  - Graceful degradation when LLM fails / no provider is available
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from ontology_pipeline.annotate import (
    CardSummary,
    KeyAreaEntry,
    SemanticVocab,
    enrich_annotations,
)
from ontology_pipeline.annotate_ner import (
    ConceptCandidate,
    build_lexicon,
    propose_causal_node_candidates,
    propose_concept_candidates,
    propose_key_area_candidates,
)
from ontology_pipeline.models import (
    AssetAnnotations,
    ColumnInfo,
    GeneratedMDL,
    TableInfo,
)
from ontology_pipeline.mdl import build_mdl


# ───────────────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────────────


def _mdl(
    name: str = "csod_employee",
    *,
    columns: list[ColumnInfo] | None = None,
    schema: str = "public",
) -> GeneratedMDL:
    table = TableInfo(
        schema_name=schema,
        name=name,
        primary_key=["id"],
        columns=columns or [
            ColumnInfo(name="id", sql_type="INTEGER", nullable=False, is_primary_key=True),
            ColumnInfo(name="employee_id", sql_type="INTEGER", nullable=True),
            ColumnInfo(name="department", sql_type="TEXT", nullable=True),
        ],
    )
    return build_mdl(source_id="csod-pg", catalog="testdb", table=table)


def _vocab(
    *,
    object_types: list[tuple[str, str | None]] | None = None,
    causal_nodes: list[tuple[str, str | None]] | None = None,
    key_areas: list[tuple[str, str | None]] | None = None,
) -> SemanticVocab:
    return SemanticVocab(
        object_types=[
            CardSummary(id=cid, kind="object_type", title=title, body_excerpt="")
            for cid, title in (object_types or [])
        ],
        causal_nodes=[
            CardSummary(id=cid, kind="causal_node", title=title, body_excerpt="")
            for cid, title in (causal_nodes or [])
        ],
        key_areas=[
            KeyAreaEntry(id=cid, description=title or "")
            for cid, title in (key_areas or [])
        ],
    )


class _StubProvider:
    """LLM stand-in. Records prompts; returns a canned JSON response."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[str] = []

    def complete(self, role, prompt, *, response_format=None):
        self.calls.append(prompt)
        return json.dumps(self._response)


# ───────────────────────────────────────────────────────────────────────────
# build_lexicon
# ───────────────────────────────────────────────────────────────────────────


class TestBuildLexicon:
    def test_includes_id_phrase_and_title(self):
        vocab = _vocab(object_types=[
            ("compliance_gap", "Compliance Gap"),
        ])
        lex = build_lexicon(vocab.object_types)
        # id verbatim, id-as-phrase (with underscores → spaces), title
        assert lex.get("compliance_gap") == "compliance_gap"
        assert lex.get("compliance gap") == "compliance_gap"

    def test_conflict_first_writer_wins(self, caplog):
        # Two cards both claim title "Employee" — second one is rejected with warning.
        vocab = _vocab(object_types=[
            ("employee", "Employee"),
            ("worker", "Employee"),
        ])
        with caplog.at_level("WARNING"):
            lex = build_lexicon(vocab.object_types)
        # Both ids land via their own id surface; the "employee" title surface
        # only points at the first card.
        assert lex["employee"] == "employee"
        assert lex["worker"] == "worker"
        # Conflict on the *title* surface only triggers the warning when both
        # cards normalize to the same surface as something already taken.
        # Here title="Employee" normalizes to "employee" which already maps to
        # "employee" id → no conflict for the first writer; the conflict is
        # the second's title trying to overwrite.
        assert any("Lexicon conflict" in r.message for r in caplog.records)

    def test_skips_items_without_id(self):
        class _Bogus:
            id = ""
        lex = build_lexicon([_Bogus()])
        assert lex == {}


# ───────────────────────────────────────────────────────────────────────────
# propose_* candidates
# ───────────────────────────────────────────────────────────────────────────


class TestProposeConcepts:
    def test_matches_table_name_to_card(self):
        # Use a table with no column whose name would also match `employee`
        # so we isolate the asset-name match path. (Column fold-in is tested
        # separately in `test_dedupe_keeps_highest_confidence`.)
        mdl = _mdl(
            name="employee",
            columns=[
                ColumnInfo(name="id", sql_type="INTEGER", nullable=False, is_primary_key=True),
                ColumnInfo(name="department", sql_type="TEXT", nullable=True),
            ],
        )
        vocab = _vocab(object_types=[("employee", "Employee")])
        cands = propose_concept_candidates(model=mdl.models[0], vocab=vocab)
        assert len(cands) == 1
        c = cands[0]
        assert c.card_id == "employee"
        assert c.kind == "object_type"
        assert c.confidence == 1.0
        assert c.match_kind == "name"
        # Asset-name match — no column evidence (matched via asset name itself)
        assert c.evidence_columns == []

    def test_matches_column_name_token(self):
        mdl = _mdl(
            name="people_directory",
            columns=[
                ColumnInfo(name="id", sql_type="INTEGER", nullable=False, is_primary_key=True),
                ColumnInfo(name="employee_id", sql_type="INTEGER", nullable=True),
            ],
        )
        vocab = _vocab(object_types=[("employee", None)])
        cands = propose_concept_candidates(model=mdl.models[0], vocab=vocab)
        # "employee" matched via token in "employee_id" column
        ids = [c.card_id for c in cands]
        assert "employee" in ids
        emp = next(c for c in cands if c.card_id == "employee")
        assert "employee_id" in emp.evidence_columns
        # Token match scores 0.70
        assert emp.confidence == 0.70
        assert emp.match_kind == "token"

    def test_stopword_token_does_not_match(self):
        # `id` is a stopword — even if a card declares id="id", we don't match
        # against the "id" PK column.
        mdl = _mdl(
            name="thing",
            columns=[
                ColumnInfo(name="id", sql_type="INTEGER", nullable=False, is_primary_key=True),
            ],
        )
        vocab = _vocab(object_types=[("id", None)])
        cands = propose_concept_candidates(model=mdl.models[0], vocab=vocab)
        # "id" appears as both the table column AND a card. Stopword filter
        # blocks the token-level match. Only direct asset-name match could land
        # but the table is "thing" not "id". So no candidates.
        assert cands == []

    def test_dedupe_keeps_highest_confidence(self):
        # Asset "employee" + column "employee_id" both point at card "employee".
        # Confidence should be the asset-name match (1.0), with column folded in.
        mdl = _mdl(
            name="employee",
            columns=[
                ColumnInfo(name="id", sql_type="INTEGER", nullable=False, is_primary_key=True),
                ColumnInfo(name="employee_id", sql_type="INTEGER", nullable=True),
            ],
        )
        vocab = _vocab(object_types=[("employee", None)])
        cands = propose_concept_candidates(model=mdl.models[0], vocab=vocab)
        assert len(cands) == 1
        assert cands[0].confidence == 1.0
        assert cands[0].match_kind == "name"
        # Column evidence still folded in
        assert "employee_id" in cands[0].evidence_columns

    def test_no_object_type_vocab_returns_empty(self):
        mdl = _mdl(name="employee")
        vocab = _vocab(object_types=[])
        cands = propose_concept_candidates(model=mdl.models[0], vocab=vocab)
        assert cands == []

    def test_id_to_phrase_match(self):
        mdl = _mdl(name="compliance gap")  # space-separated
        vocab = _vocab(object_types=[("compliance_gap", None)])
        cands = propose_concept_candidates(model=mdl.models[0], vocab=vocab)
        # Lexicon registers both "compliance_gap" and "compliance gap"; the
        # asset name "compliance gap" normalises to the latter.
        assert [c.card_id for c in cands] == ["compliance_gap"]


class TestProposeKeyAreas:
    def test_key_area_lexicon_used(self):
        mdl = _mdl(name="hipaa_audit_log")
        vocab = _vocab(key_areas=[("hipaa", "HIPAA compliance")])
        cands = propose_key_area_candidates(model=mdl.models[0], vocab=vocab)
        ids = [c.card_id for c in cands]
        assert "hipaa" in ids
        assert cands[0].kind == "key_area"


class TestProposeCausalNodes:
    def test_causal_node_lexicon_used(self):
        mdl = _mdl(
            name="employee",
            columns=[
                ColumnInfo(name="id", sql_type="INTEGER", nullable=False, is_primary_key=True),
                ColumnInfo(name="compliance_gap_days", sql_type="INTEGER", nullable=True),
            ],
        )
        vocab = _vocab(causal_nodes=[("compliance_gap", None)])
        cands = propose_causal_node_candidates(model=mdl.models[0], vocab=vocab)
        assert [c.card_id for c in cands] == ["compliance_gap"]
        assert cands[0].kind == "causal_node"


# ───────────────────────────────────────────────────────────────────────────
# enrich_annotations — three modes
# ───────────────────────────────────────────────────────────────────────────


class TestEnrichAnnotationsNerOnly:
    def test_emits_ner_candidates_without_calling_llm(self):
        mdl = _mdl(name="employee")
        vocab = _vocab(object_types=[("employee", None)])
        provider = _StubProvider({"concepts": ["wrong"], "key_areas": [], "causal_relations": [], "confidence": 0.9, "rationale": "x"})

        result = enrich_annotations(
            mdl, vocab=vocab, provider=provider, concepts_source="ner_only",
        )
        assert isinstance(result, AssetAnnotations)
        assert result.concepts == ["employee"]
        assert result.source == "ner_pre_pass"
        # LLM not called
        assert provider.calls == []

    def test_returns_none_when_ner_finds_nothing(self):
        mdl = _mdl(name="random_table")
        vocab = _vocab(object_types=[("nothing_matches", None)])
        result = enrich_annotations(
            mdl, vocab=vocab, provider=None, concepts_source="ner_only",
        )
        assert result is None


class TestEnrichAnnotationsLlmOnly:
    def test_prompt_has_no_ner_grounding_block(self):
        mdl = _mdl(name="employee")
        vocab = _vocab(
            object_types=[("employee", None), ("worker", None)],
        )
        provider = _StubProvider({
            "concepts": ["employee"], "key_areas": [], "causal_relations": [],
            "confidence": 0.9, "rationale": "best guess",
        })
        result = enrich_annotations(
            mdl, vocab=vocab, provider=provider, concepts_source="llm_only",
        )
        assert result is not None
        assert result.concepts == ["employee"]
        # No NER grounding block in prompt
        assert "NER GROUNDING" not in provider.calls[0]
        assert result.source == "llm_enrichment"


class TestEnrichAnnotationsNerThenLlm:
    def test_prompt_includes_ner_grounding(self):
        mdl = _mdl(name="employee")
        vocab = _vocab(object_types=[("employee", None)])
        provider = _StubProvider({
            "concepts": ["employee"], "key_areas": [], "causal_relations": [],
            "confidence": 0.9, "rationale": "confirmed",
        })
        result = enrich_annotations(
            mdl, vocab=vocab, provider=provider,
            concepts_source="ner_then_llm",
        )
        assert result is not None
        assert result.concepts == ["employee"]
        assert result.source == "ner_then_llm"
        prompt = provider.calls[0]
        assert "NER GROUNDING" in prompt
        # The candidate is in the prompt
        assert "employee" in prompt
        # Match-kind annotation appears
        assert "match=name" in prompt

    def test_llm_can_override_ner(self):
        # NER proposes 'employee'; LLM picks 'worker' instead. LLM wins since
        # 'worker' is in the candidate cards too.
        mdl = _mdl(name="employee")
        vocab = _vocab(object_types=[("employee", None), ("worker", None)])
        provider = _StubProvider({
            "concepts": ["worker"], "key_areas": [], "causal_relations": [],
            "confidence": 0.85, "rationale": "I disagree with NER",
        })
        result = enrich_annotations(
            mdl, vocab=vocab, provider=provider,
            concepts_source="ner_then_llm",
        )
        assert result is not None
        assert result.concepts == ["worker"]


class TestEnrichAnnotationsGracefulDegradation:
    def test_llm_failure_falls_back_to_ner_in_hybrid_mode(self):
        class _Failing:
            def complete(self, *a, **kw): raise RuntimeError("api down")
        mdl = _mdl(name="employee")
        vocab = _vocab(object_types=[("employee", None)])
        result = enrich_annotations(
            mdl, vocab=vocab, provider=_Failing(),
            concepts_source="ner_then_llm",
        )
        # NER had results → fallback succeeds
        assert result is not None
        assert result.concepts == ["employee"]
        assert result.source == "ner_pre_pass"

    def test_no_provider_in_hybrid_mode_uses_ner(self):
        mdl = _mdl(name="employee")
        vocab = _vocab(object_types=[("employee", None)])
        result = enrich_annotations(
            mdl, vocab=vocab, provider=None,
            concepts_source="ner_then_llm",
        )
        assert result is not None
        assert result.concepts == ["employee"]
        assert result.source == "ner_pre_pass"

    def test_llm_failure_returns_none_when_ner_also_empty(self):
        class _Failing:
            def complete(self, *a, **kw): raise RuntimeError("api down")
        mdl = _mdl(name="random_table")
        vocab = _vocab(object_types=[("nothing_matches", None)])
        result = enrich_annotations(
            mdl, vocab=vocab, provider=_Failing(),
            concepts_source="ner_then_llm",
        )
        assert result is None

    def test_empty_vocab_returns_none_regardless_of_mode(self):
        mdl = _mdl(name="employee")
        empty_vocab = _vocab()
        for mode in ("ner_only", "ner_then_llm", "llm_only"):
            provider = _StubProvider({"concepts": [], "key_areas": [], "causal_relations": [], "confidence": 0.0, "rationale": ""})
            assert enrich_annotations(
                mdl, vocab=empty_vocab, provider=provider, concepts_source=mode,
            ) is None
