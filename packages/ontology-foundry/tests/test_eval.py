import pytest

from ontology_foundry.eval import (
    GateVerdict,
    check_path_shapley_sum,
    check_quantitative_integrity,
    check_reported_weight_matches_card,
    context_precision_recall,
    directed_graph_has_cycle,
    gate_nonempty_body,
    gate_refs_resolve,
    regression_gate_quality,
    regression_gate_zero_tolerance,
    score_span_grounding,
)


def test_score_span_grounding_numbers() -> None:
    r = score_span_grounding(
        "Training reduces phishing success by ~40%",
        "Phishing simulation training reduces successful attempts by ~40% in Q3.",
        min_lexical=0.08,
    )
    assert r.numbers_aligned
    assert r.passed


def test_regression_gate_quality() -> None:
    rep = regression_gate_quality("faithfulness", 0.96, 0.955, max_drop_pp=0.01)
    assert rep.allowed
    rep2 = regression_gate_quality("faithfulness", 0.96, 0.94, max_drop_pp=0.01)
    assert not rep2.allowed


def test_regression_zero_tolerance() -> None:
    assert regression_gate_zero_tolerance("probe_fail_rate", 0.02, 0.02).allowed
    assert not regression_gate_zero_tolerance("probe_fail_rate", 0.02, 0.03).allowed


def test_retrieval_metrics() -> None:
    m = context_precision_recall({"a", "b", "c"}, {"a", "d"})
    assert m.context_precision == pytest.approx(1 / 3)
    assert m.context_recall == pytest.approx(0.5)


def test_path_shapley() -> None:
    assert check_path_shapley_sum([60.0, 40.0])
    assert not check_path_shapley_sum([60.0, 50.0])


def test_causal_weight_match() -> None:
    r = check_reported_weight_matches_card(0.72, 0.72, reported_ci=(0.5, 0.9), card_ci=(0.5, 0.9))
    assert r.weight_aligned and r.ci_aligned


def test_cycle_detection() -> None:
    assert not directed_graph_has_cycle([("a", "b"), ("b", "c")])
    assert directed_graph_has_cycle([("a", "b"), ("b", "c"), ("c", "a")])


def test_gates() -> None:
    assert gate_nonempty_body("x")[0] == GateVerdict.PASS
    assert gate_nonempty_body("  ")[0] == GateVerdict.FAIL
    assert gate_refs_resolve("c1", ["x"], {"x"})[0] == GateVerdict.PASS
    assert gate_refs_resolve("c1", ["y"], {"x"})[0] == GateVerdict.FAIL


def test_quant_integrity() -> None:
    q = check_quantitative_integrity(0.38, 0.40, relative_tolerance=0.05)
    assert q.aligned
