from __future__ import annotations

import duckdb
import duckdb.func

from nexcraft_jobs.compute.udfs.sql_moving_averages import (
    calculate_bollinger_bands_arrow,
    calculate_cumulative_operations_arrow,
    calculate_ema_json_arrow,
    calculate_expanding_window_arrow,
    calculate_moving_correlation_arrow,
    calculate_moving_minmax_arrow,
    calculate_moving_quantiles_arrow,
    calculate_moving_rank_arrow,
    calculate_moving_sum_arrow,
    calculate_moving_variance_arrow,
    calculate_sma_arrow,
    calculate_time_weighted_ma_arrow,
    calculate_wma_arrow,
)
from nexcraft_jobs.compute.udfs.mltools_json import (
    ml_cohort_retention_json_arrow,
    ml_funnel_json_arrow,
    ml_metrics_summary_json_arrow,
    ml_segment_kmeans_json_arrow,
    ml_trend_linear_json_arrow,
)
from nexcraft_jobs.compute.udfs.timeseries import ema_arrow
from nexcraft_jobs.compute.udfs.mltools.registry import register_mltools_udfs

_TS = "TIMESTAMP"
_LIST = "[]"

# Return types: DuckDB ``STRUCT(...)[]`` for table-valued SQL functions (unwrap with ``unnest``).
_RT_SMA = (
    f"STRUCT(row_number INTEGER, time_period {_TS}, original_value DOUBLE, sma_value DOUBLE, "
    f"deviation DOUBLE, percent_deviation DOUBLE, upper_band DOUBLE, lower_band DOUBLE){_LIST}"
)
_RT_WMA = (
    f"STRUCT(row_number INTEGER, time_period {_TS}, original_value DOUBLE, wma_value DOUBLE, "
    f"deviation DOUBLE, percent_deviation DOUBLE){_LIST}"
)
_RT_MV = (
    f"STRUCT(row_number INTEGER, time_period {_TS}, original_value DOUBLE, moving_mean DOUBLE, "
    f"moving_variance DOUBLE, moving_std DOUBLE, coefficient_variation DOUBLE, z_score DOUBLE){_LIST}"
)
_RT_MQ = (
    f"STRUCT(row_number INTEGER, time_period {_TS}, original_value DOUBLE, q25 DOUBLE, "
    f"q50_median DOUBLE, q75 DOUBLE, iqr DOUBLE){_LIST}"
)
_RT_MM = (
    f"STRUCT(row_number INTEGER, time_period {_TS}, original_value DOUBLE, moving_min DOUBLE, "
    f"moving_max DOUBLE, moving_range DOUBLE, position_in_range DOUBLE){_LIST}"
)
_RT_MCORR = (
    f"STRUCT(row_number INTEGER, time_period {_TS}, value_x DOUBLE, value_y DOUBLE, "
    f"correlation DOUBLE, correlation_strength VARCHAR){_LIST}"
)
_RT_MSUM = (
    f"STRUCT(row_number INTEGER, time_period {_TS}, original_value DOUBLE, moving_sum DOUBLE, "
    f"contribution_pct DOUBLE){_LIST}"
)
_RT_EXPAND = (
    f"STRUCT(row_number INTEGER, time_period {_TS}, original_value DOUBLE, expanding_value DOUBLE, "
    f"window_size INTEGER){_LIST}"
)
_RT_CUM = (
    f"STRUCT(row_number INTEGER, time_period {_TS}, original_value DOUBLE, cumsum DOUBLE, "
    f"cumproduct DOUBLE, cummax DOUBLE, cummin DOUBLE, percent_of_total DOUBLE){_LIST}"
)
_RT_TWMA = (
    f"STRUCT(row_number INTEGER, time_period {_TS}, original_value DOUBLE, twma_value DOUBLE, "
    f"deviation DOUBLE){_LIST}"
)
_RT_BB = (
    f"STRUCT(row_number INTEGER, time_period {_TS}, original_value DOUBLE, middle_band DOUBLE, "
    f"upper_band DOUBLE, lower_band DOUBLE, bandwidth DOUBLE, percent_b DOUBLE){_LIST}"
)
_RT_MRANK = (
    f"STRUCT(row_number INTEGER, time_period {_TS}, original_value DOUBLE, window_rank INTEGER, "
    f"window_percentile DOUBLE, is_highest BOOLEAN, is_lowest BOOLEAN){_LIST}"
)
_RT_EMAJ = (
    f"STRUCT(row_number INTEGER, time_period {_TS}, original_value DOUBLE, ema_value DOUBLE, "
    f"deviation DOUBLE){_LIST}"
)


def register_analytical_udfs(con: duckdb.DuckDBPyConnection) -> None:
    """Register Arrow / native Python UDFs (moving windows, timeseries helpers, mltools JSON, optional STL)."""
    arrow = duckdb.func.PythonUDFType.ARROW

    con.create_function("ema", ema_arrow, ["DOUBLE[]", "DOUBLE"], "DOUBLE[]", type=arrow)

    # --- Moving averages & windows (``moving_averages_functions.sql``; ``p_data`` = VARCHAR JSON) ---
    con.create_function(
        "calculate_sma", calculate_sma_arrow, ["VARCHAR", "INTEGER", "VARCHAR"], _RT_SMA, type=arrow
    )
    con.create_function(
        "calculate_wma", calculate_wma_arrow, ["VARCHAR", "INTEGER", "VARCHAR"], _RT_WMA, type=arrow
    )
    con.create_function(
        "calculate_moving_variance",
        calculate_moving_variance_arrow,
        ["VARCHAR", "INTEGER", "VARCHAR"],
        _RT_MV,
        type=arrow,
    )
    con.create_function(
        "calculate_moving_quantiles",
        calculate_moving_quantiles_arrow,
        ["VARCHAR", "INTEGER", "DOUBLE[]", "VARCHAR"],
        _RT_MQ,
        type=arrow,
    )
    con.create_function(
        "calculate_moving_minmax",
        calculate_moving_minmax_arrow,
        ["VARCHAR", "INTEGER", "VARCHAR"],
        _RT_MM,
        type=arrow,
    )
    con.create_function(
        "calculate_moving_correlation",
        calculate_moving_correlation_arrow,
        ["VARCHAR", "VARCHAR", "INTEGER"],
        _RT_MCORR,
        type=arrow,
    )
    con.create_function(
        "calculate_moving_sum",
        calculate_moving_sum_arrow,
        ["VARCHAR", "INTEGER", "VARCHAR"],
        _RT_MSUM,
        type=arrow,
    )
    con.create_function(
        "calculate_expanding_window",
        calculate_expanding_window_arrow,
        ["VARCHAR", "VARCHAR", "VARCHAR"],
        _RT_EXPAND,
        type=arrow,
    )
    con.create_function(
        "calculate_cumulative_operations",
        calculate_cumulative_operations_arrow,
        ["VARCHAR", "VARCHAR", "VARCHAR"],
        _RT_CUM,
        type=arrow,
    )
    con.create_function(
        "calculate_time_weighted_ma",
        calculate_time_weighted_ma_arrow,
        ["VARCHAR", "DOUBLE", "INTEGER"],
        _RT_TWMA,
        type=arrow,
    )
    con.create_function(
        "calculate_bollinger_bands",
        calculate_bollinger_bands_arrow,
        ["VARCHAR", "INTEGER", "DOUBLE"],
        _RT_BB,
        type=arrow,
    )
    con.create_function(
        "calculate_moving_rank",
        calculate_moving_rank_arrow,
        ["VARCHAR", "INTEGER", "VARCHAR"],
        _RT_MRANK,
        type=arrow,
    )

    # --- ``timeseries_analysis_functions.sql`` / overlap: JSON ``calculate_ema`` ---
    con.create_function(
        "calculate_ema", calculate_ema_json_arrow, ["VARCHAR", "DOUBLE", "VARCHAR"], _RT_EMAJ, type=arrow
    )

    # --- ``mltools``-aligned JSON analytics (numpy-only; no pandas in nexcraft-jobs) ---
    con.create_function(
        "ml_funnel_json",
        ml_funnel_json_arrow,
        ["VARCHAR", "VARCHAR", "VARCHAR", "VARCHAR"],
        "VARCHAR",
        type=arrow,
    )
    con.create_function(
        "ml_cohort_retention_json",
        ml_cohort_retention_json_arrow,
        ["VARCHAR", "VARCHAR", "VARCHAR", "VARCHAR"],
        "VARCHAR",
        type=arrow,
    )
    con.create_function(
        "ml_metrics_summary_json",
        ml_metrics_summary_json_arrow,
        ["VARCHAR"],
        "VARCHAR",
        type=arrow,
    )
    con.create_function(
        "ml_segment_kmeans_json",
        ml_segment_kmeans_json_arrow,
        ["VARCHAR", "INTEGER", "VARCHAR"],
        "VARCHAR",
        type=arrow,
    )
    con.create_function(
        "ml_trend_linear_json",
        ml_trend_linear_json_arrow,
        ["VARCHAR"],
        "VARCHAR",
        type=arrow,
    )

    try:
        import statsmodels  # noqa: F401

        from nexcraft_jobs.compute.udfs.timeseries import stl_decompose_arrow

        con.create_function(
            "stl_decompose",
            stl_decompose_arrow,
            ["DOUBLE[]", "INTEGER"],
            "STRUCT(trend DOUBLE[], seasonal DOUBLE[], resid DOUBLE[])",
            type=arrow,
        )
    except ImportError:
        pass

    register_mltools_udfs(con)
