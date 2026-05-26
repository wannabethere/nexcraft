from __future__ import annotations

from typing import Callable, Sequence

from ontology_foundry.analysis.models import CorrelationFinding

PairFn = Callable[[Sequence[float], Sequence[float]], tuple[float, float | None]]


def _require_scipy() -> None:
    try:
        import scipy  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "Correlation helpers require SciPy. Install optional dependency: ontology-foundry[analysis]"
        ) from e


def pearson_pair_fn() -> PairFn:
    _require_scipy()
    from scipy import stats as st

    def fn(x: Sequence[float], y: Sequence[float]) -> tuple[float, float | None]:
        r, p = st.pearsonr(x, y)
        return float(r), float(p)

    return fn


def spearman_pair_fn() -> PairFn:
    _require_scipy()
    from scipy import stats as st

    def fn(x: Sequence[float], y: Sequence[float]) -> tuple[float, float | None]:
        r, p = st.spearmanr(x, y)
        return float(r), float(p)

    return fn


def linear_pearson_pair(
    column_a: str,
    column_b: str,
    x: Sequence[float],
    y: Sequence[float],
    *,
    alpha: float = 0.05,
    min_effect: float = 0.0,
) -> CorrelationFinding | None:
    """§3.3 `LinearCorrelator` — Pearson / numeric ↔ numeric."""
    fn = pearson_pair_fn()
    r, p = fn(x, y)
    n = len(x)
    sig = p is not None and p < alpha and abs(r) >= min_effect
    if not sig:
        return None
    return CorrelationFinding(
        column_a=column_a,
        column_b=column_b,
        method="pearson",
        effect_size=r,
        p_value=p,
        n=n,
        significant=True,
    )


def rank_spearman_pair(
    column_a: str,
    column_b: str,
    x: Sequence[float],
    y: Sequence[float],
    *,
    alpha: float = 0.05,
    min_effect: float = 0.0,
) -> CorrelationFinding | None:
    """§3.3 `RankCorrelator` — Spearman."""
    fn = spearman_pair_fn()
    r, p = fn(x, y)
    n = len(x)
    sig = p is not None and p < alpha and abs(r) >= min_effect
    if not sig:
        return None
    return CorrelationFinding(
        column_a=column_a,
        column_b=column_b,
        method="spearman",
        effect_size=r,
        p_value=p,
        n=n,
        significant=True,
    )


def mutual_information_sklearn(
    column_a: str,
    column_b: str,
    x: Sequence[float],
    y: Sequence[float],
    *,
    min_effect: float = 0.01,
) -> CorrelationFinding | None:
    """§3.3 `MICorrelator` — sklearn mutual_info_regression as an MI proxy."""
    try:
        import numpy as np
        from sklearn.feature_selection import mutual_info_regression
    except ImportError as e:
        raise ImportError(
            "mutual_information_sklearn requires numpy and scikit-learn. "
            "Install ontology-foundry[analysis]"
        ) from e

    xi = np.asarray(x, dtype=float).reshape(-1, 1)
    yi = np.asarray(y, dtype=float)
    mi = float(mutual_info_regression(xi, yi, random_state=0)[0])
    if mi < min_effect:
        return None
    return CorrelationFinding(
        column_a=column_a,
        column_b=column_b,
        method="mutual_information",
        effect_size=mi,
        p_value=None,
        n=len(x),
        significant=True,
        diagnostics={"library": "sklearn.feature_selection.mutual_info_regression"},
    )


def pairwise_numeric_screen(
    columns: dict[str, Sequence[float]],
    *,
    method: str = "pearson",
    alpha: float = 0.05,
    min_effect: float = 0.1,
) -> list[CorrelationFinding]:
    """
    Cartesian screen over numeric columns (standalone backend); Spark/Ray partition
    this differently at scale (extraction §7–8).
    """
    names = list(columns.keys())
    out: list[CorrelationFinding] = []
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            xa, xb = columns[a], columns[b]
            if len(xa) != len(xb):
                continue
            if method == "pearson":
                found = linear_pearson_pair(a, b, xa, xb, alpha=alpha, min_effect=min_effect)
            elif method == "spearman":
                found = rank_spearman_pair(a, b, xa, xb, alpha=alpha, min_effect=min_effect)
            else:
                raise ValueError(f"Unsupported method for screen: {method}")
            if found is not None:
                out.append(found)
    return out
