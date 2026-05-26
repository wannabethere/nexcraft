from __future__ import annotations

import math
from collections import Counter
from typing import Sequence

from ontology_foundry.analysis.models import BootstrapResult, NumericColumnProfile


def profile_numeric_column(name: str, values: Sequence[float | None]) -> NumericColumnProfile:
    """§3.2 column profiler — numeric subset without heavy histogram/HLL."""
    present = [float(v) for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    n = len(values)
    if n == 0:
        return NumericColumnProfile(column=name, n_rows=0, null_rate=1.0)
    null_rate = 1.0 - (len(present) / n)
    distinct_count = len(set(present))
    if not present:
        return NumericColumnProfile(
            column=name,
            n_rows=n,
            null_rate=null_rate,
            distinct_count=0,
        )
    mean = sum(present) / len(present)
    variance = sum((x - mean) ** 2 for x in present) / max(1, len(present) - 1)
    std = math.sqrt(variance)
    return NumericColumnProfile(
        column=name,
        n_rows=n,
        null_rate=null_rate,
        distinct_count=distinct_count,
        mean=mean,
        std=std,
        min=min(present),
        max=max(present),
    )


def profile_categorical_column(name: str, values: Sequence[object | None]) -> NumericColumnProfile:
    """Null rate + distinct count for strings/categories (cheap path)."""
    n = len(values)
    if n == 0:
        return NumericColumnProfile(column=name, n_rows=0, null_rate=1.0)
    non_null = [v for v in values if v is not None]
    null_rate = 1.0 - (len(non_null) / n)
    distinct_count = len(set(non_null))
    return NumericColumnProfile(
        column=name,
        n_rows=n,
        null_rate=null_rate,
        distinct_count=distinct_count,
    )


def bootstrap_ci(
    values: Sequence[float],
    *,
    n_bootstrap: int = 1000,
    alpha: float = 0.05,
    seed: int = 0,
) -> BootstrapResult:
    """§4.1 statistical models — bootstrap CI for the mean via `scipy.stats.bootstrap`."""
    arr_list = [float(v) for v in values]
    n = len(arr_list)
    if n == 0:
        raise ValueError("bootstrap_ci requires at least one value")

    try:
        import numpy as np
        from scipy.stats import bootstrap
    except ImportError as e:
        raise ImportError(
            "bootstrap_ci requires SciPy and NumPy. Install ontology-foundry[analysis]"
        ) from e

    rng = np.random.default_rng(seed)
    arr = np.asarray(arr_list, dtype=float)

    def stat_mean(sample: np.ndarray, axis: int = -1) -> np.floating:
        return np.mean(sample, axis=axis)

    res = bootstrap(
        (arr,),
        stat_mean,
        vectorized=True,
        n_resamples=n_bootstrap,
        rng=rng,
        confidence_level=1.0 - alpha,
    )
    ci = res.confidence_interval
    low = float(ci.low if hasattr(ci, "low") else ci[0])
    high = float(ci.high if hasattr(ci, "high") else ci[1])
    return BootstrapResult(
        statistic_name="mean",
        point_estimate=float(np.mean(arr)),
        ci_low=low,
        ci_high=high,
        n_bootstrap=n_bootstrap,
    )


def top_k_freq(values: Sequence[object | None], k: int = 5) -> list[tuple[object, int]]:
    """Top-k frequency table for categorical/string profiling."""
    counts = Counter(v for v in values if v is not None)
    return counts.most_common(k)
