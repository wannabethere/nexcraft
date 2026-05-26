"""Moving-window family aligned with genieml ``movingaverages`` — implemented in ``sql_moving_averages`` and ``invoke_sql_function``."""

from __future__ import annotations

FUNCTION_NAMES: frozenset[str] = frozenset(
    {
        "calculate_sma",
        "calculate_wma",
        "calculate_moving_variance",
        "calculate_moving_quantiles",
        "calculate_moving_minmax",
        "calculate_moving_correlation",
        "calculate_moving_sum",
        "calculate_expanding_window",
        "calculate_cumulative_operations",
        "calculate_time_weighted_ma",
        "calculate_bollinger_bands",
        "calculate_moving_rank",
        "calculate_ema",
        "calculate_moving_average",
    }
)

__all__ = ["FUNCTION_NAMES"]
