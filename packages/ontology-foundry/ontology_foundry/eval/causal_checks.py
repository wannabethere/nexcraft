from __future__ import annotations

from ontology_foundry.eval.models import CausalResponseCheckResult


def check_path_shapley_sum(
    path_contributions_percent: list[float],
    *,
    tol: float = 1e-3,
) -> bool:
    """eval_strategy §6.5 — attributions sum to 100%."""
    return abs(sum(path_contributions_percent) - 100.0) <= tol


def check_reported_weight_matches_card(
    reported_weight: float | None,
    card_weight: float | None,
    *,
    reported_ci: tuple[float, float] | None = None,
    card_ci: tuple[float, float] | None = None,
    weight_tol: float = 1e-6,
    ci_tol: float = 1e-6,
) -> CausalResponseCheckResult:
    """eval_strategy §8.3 — reported vs underlying causal_edge."""
    w_ok = (
        reported_weight is not None
        and card_weight is not None
        and abs(reported_weight - card_weight) <= weight_tol
    )
    ci_ok = True
    rl, rh = None, None
    cl, ch = None, None
    if reported_ci is not None and card_ci is not None:
        rl, rh = reported_ci
        cl, ch = card_ci
        ci_ok = abs(rl - cl) <= ci_tol and abs(rh - ch) <= ci_tol
    return CausalResponseCheckResult(
        reported_weight=reported_weight,
        card_weight=card_weight,
        weight_aligned=w_ok,
        reported_ci_low=rl,
        reported_ci_high=rh,
        card_ci_low=cl,
        card_ci_high=ch,
        ci_aligned=ci_ok if reported_ci is not None else False,
    )


def directed_graph_has_cycle(edges: list[tuple[str, str]]) -> bool:
    """eval_strategy §4 — cross-card causal DAG cycle check."""
    adj: dict[str, list[str]] = {}
    nodes: set[str] = set()
    for u, v in edges:
        nodes.add(u)
        nodes.add(v)
        adj.setdefault(u, []).append(v)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in nodes}

    def visit(u: str) -> bool:
        color[u] = GRAY
        for v in adj.get(u, []):
            if color.get(v, WHITE) == GRAY:
                return True
            if color.get(v, WHITE) == WHITE and visit(v):
                return True
        color[u] = BLACK
        return False

    for n in nodes:
        if color[n] == WHITE and visit(n):
            return True
    return False
