from __future__ import annotations

from ontology_foundry.eval.models import RegressionGateReport


def regression_gate_quality(
    metric_name: str,
    baseline: float,
    current: float,
    *,
    max_drop_pp: float,
) -> RegressionGateReport:
    """
    eval_strategy §9.2 — quality metric cannot drop more than max_drop_pp percentage points.
    """
    drop = baseline - current
    allowed = drop <= max_drop_pp
    return RegressionGateReport(
        metric_name=metric_name,
        baseline=baseline,
        current=current,
        max_regression=max_drop_pp,
        zero_tolerance=False,
        allowed=allowed,
    )


def regression_gate_zero_tolerance(
    metric_name: str,
    baseline: float,
    current: float,
) -> RegressionGateReport:
    """Hallucination probe rate etc. — any regression fails."""
    allowed = current <= baseline + 1e-12
    return RegressionGateReport(
        metric_name=metric_name,
        baseline=baseline,
        current=current,
        zero_tolerance=True,
        allowed=allowed,
    )
