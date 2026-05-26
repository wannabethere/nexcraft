"""Time-series transforms aligned with genieml ``timeseriesanalysis`` — use ``list_timeseries_to_json`` + ``invoke_sql_function``."""

from __future__ import annotations

FUNCTION_NAMES: frozenset[str] = frozenset(
    {
        "calculate_lag",
        "calculate_lead",
        "calculate_growth_rates",
        "calculate_statistical_trend",
        "forecast_linear",
        "calculate_volatility",
        "detect_seasonality",
        "calculate_cumulative_trend",
        "classify_trend",
        "analyze_variance",
        "calculate_difference",
        "calculate_cdf",
        "calculate_rolling_window",
        "calculate_autocorrelation",
        "test_stationarity",
        "calculate_cumulative",
        "calculate_percent_change",
        "compare_periods",
    }
)

__all__ = ["FUNCTION_NAMES"]
