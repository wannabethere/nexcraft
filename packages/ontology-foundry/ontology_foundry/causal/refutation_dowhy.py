from __future__ import annotations

from typing import Any

from ontology_foundry.causal.models import RefutationSummary


def refute_random_common_cause(
    data: Any,
    *,
    treatment: str,
    outcome: str,
    graph_dot: str,
    estimate_method: str = "backdoor.linear_regression",
) -> tuple[RefutationSummary, Any]:
    """
    Thin DoWhy wrapper — random common-cause refutation (ingestion §5.3 DoWhy path).

    `data` should be a pandas DataFrame; `graph_dot` a DOT string describing the DAG.
    """
    try:
        from dowhy import CausalModel
    except ImportError as e:
        raise ImportError("DoWhy refutation requires dowhy. Install: ontology-foundry[causal]") from e

    model = CausalModel(
        data=data,
        treatment=treatment,
        outcome=outcome,
        graph=graph_dot,
    )
    estimand = model.identify_estimand(proceed_when_unidentifiable=True)
    estimate = model.estimate_effect(estimand, method_name=estimate_method)
    ref = model.refute_estimate(estimate, method_name="random_common_cause")

    invalid = getattr(ref, "refutation_result", None)
    summary = RefutationSummary(
        refutation_type="random_common_cause",
        is_invalid=bool(invalid) if invalid is not None else None,
        refutation_result=invalid,
        diagnostics={"estimate_method": estimate_method},
    )
    return summary, ref
