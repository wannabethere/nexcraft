from __future__ import annotations

from typing import Any

import numpy as np

from ontology_foundry.causal.models import GrangerFinding


def granger_pair(
    cause: np.ndarray,
    effect: np.ndarray,
    *,
    cause_name: str = "cause",
    effect_name: str = "effect",
    max_lag: int = 4,
    alpha: float = 0.05,
) -> GrangerFinding:
    """
    Bivariate Granger test via `statsmodels` (ingestion §5.3 Granger row).
    """
    try:
        from statsmodels.tsa.stattools import grangercausalitytests
    except ImportError as e:
        raise ImportError(
            "Granger causality requires statsmodels. Install: ontology-foundry[timeseries]"
        ) from e

    x = np.asarray(cause, dtype=float).ravel()
    y = np.asarray(effect, dtype=float).ravel()
    n = min(len(x), len(y))
    if n < max_lag * 3:
        raise ValueError("series too short for Granger test at this lag order")
    stacked = np.column_stack([y[:n], x[:n]])

    results = grangercausalitytests(stacked, maxlag=max_lag, verbose=False)
    best_lag: int | None = None
    min_p = 1.0
    for lag in sorted(results.keys()):
        pack = results[lag]
        res0 = pack[0]
        test_result = res0.get("ssr_ftest")
        if test_result is None:
            continue
        p = float(test_result[1])
        if p < min_p:
            min_p = p
            best_lag = int(lag)

    return GrangerFinding(
        cause_column=cause_name,
        effect_column=effect_name,
        max_lag=max_lag,
        min_p_value=min_p,
        best_lag=best_lag,
        significant=min_p < alpha,
    )
