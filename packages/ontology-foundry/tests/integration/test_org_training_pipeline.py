"""Integration test for :mod:`ontology_foundry.integration.org_training_pipeline`."""

from __future__ import annotations

from ontology_foundry.integration import run_org_training_scenario_extractable


def test_org_training_pipeline_produces_extractable_bundle() -> None:
    bundle = run_org_training_scenario_extractable()

    assert bundle["dataset"]["seed_concepts"]
    assert bundle["tabular"]["profiles"]["tenure_months"]["n_rows"] == 180
    assert bundle["tabular"]["tier1_candidate_pair"] is not None
    tb = bundle["tabular"]["tier1_candidate_pair"]
    assert isinstance(tb, dict)
    assert tb.get("column_a") == "tenure_months"

    doc = bundle["document"]
    assert isinstance(doc, dict)
    assert doc.get("document_id") == "policy-security-training-001"
    assert doc.get("span_artifacts")

    lex = bundle["lexical_retrieval_pass"]
    assert lex.get("retrieval_hits")

    summ = bundle["summary"]
    assert summ["span_count"] >= 1
