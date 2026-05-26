"""Generate docs/ml_tools_duckdb_sql_examples.md from vendored sql_functions.json. Run: python docs/_gen_ml_tools_sql_examples.py"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAT = ROOT / "packages/nexcraft-jobs/nexcraft_jobs/compute/udfs/data/sql_functions.json"
OUT = ROOT / "docs" / "ml_tools_duckdb_sql_examples.md"

TS = '[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}]'
TS_METRIC = '[{"time":"2024-01-01T00:00:00","metric":100.0},{"time":"2024-01-02T00:00:00","metric":110.0},{"time":"2024-01-03T00:00:00","metric":105.0}]'
PANEL = (
    '[{"metric_date":"2024-01-01","metric_name":"revenue","metric_value":10.0,"region":"east"},'
    '{"metric_date":"2024-01-02","metric_name":"latency","metric_value":50.0,"region":"east"},'
    '{"metric_date":"2024-01-01","metric_name":"revenue","metric_value":12.0,"region":"west"}]'
)


def esc_sql_str(s: str) -> str:
    return s.replace("'", "''")


def payload_for(fn: str) -> dict:
    base = {"p_data": json.loads(TS)}
    mbase = {"p_data": json.loads(TS_METRIC)}
    if fn == "find_correlated_metrics":
        return {"p_primary_metric": "revenue", "panel": json.loads(PANEL), "p_min_correlation": 0.5}
    if fn == "calculate_lag_correlation":
        return {"p_primary_metric": "revenue", "panel": json.loads(PANEL)}
    if fn == "decompose_impact_by_dimension":
        return {"p_metric_name": "revenue", "p_dimension": "region", "panel": json.loads(PANEL)}
    if fn == "build_anomaly_explanation_payload":
        return {"p_primary_metric": "revenue", "p_anomaly_date": "2024-01-15", "panel": json.loads(PANEL)}
    if fn in (
        "calculate_sma",
        "calculate_wma",
        "calculate_moving_variance",
        "calculate_moving_minmax",
        "calculate_moving_sum",
        "calculate_moving_rank",
        "calculate_expanding_window",
    ):
        return {**base, "p_window_size": 2, "p_group_by": ""}
    if fn == "calculate_moving_quantiles":
        return {**base, "p_window_size": 3, "p_quantiles": [0.25, 0.5, 0.75], "p_group_by": ""}
    if fn == "calculate_moving_correlation":
        return {"p_data_x": json.loads(TS), "p_data_y": json.loads(TS), "p_window_size": 2}
    if fn == "calculate_cumulative_operations":
        return {**base, "p_operations": ["sum", "product", "max", "min"], "p_group_by": ""}
    if fn == "calculate_time_weighted_ma":
        return {**base, "p_decay_factor": 0.1, "p_window_size": 30}
    if fn == "calculate_bollinger_bands":
        return {**base, "p_window_size": 2, "p_num_std": 2.0}
    if fn == "calculate_ema":
        return {**base, "p_alpha": 0.3, "p_group_by": ""}
    if fn in ("calculate_percent_change_comparison", "calculate_absolute_change_comparison"):
        return {
            "p_data": [
                {"condition": "control", "metric": "conversion", "value": 0.10},
                {"condition": "treatment", "metric": "conversion", "value": 0.12},
            ],
            "p_condition_column": "condition",
            "p_baseline_value": "control",
        }
    if fn in ("calculate_prepost_comparison", "calculate_stratified_analysis", "calculate_sequential_analysis", "calculate_cuped_adjustment"):
        return {"p_data": []}
    if fn == "calculate_bootstrap_ci":
        return {
            "p_data": [{"value": 100}, {"value": 105}, {"value": 110}],
            "p_metric": "mean",
            "p_confidence_level": 95,
            "p_bootstrap_samples": 200,
        }
    if fn == "calculate_power_analysis":
        return {"p_effect_size": 10.0, "p_baseline_std": 25.0, "p_alpha": 0.05, "p_power": 0.8}
    if fn == "calculate_effect_sizes":
        return {"p_data_treatment": [{"value": 105}, {"value": 110}], "p_data_control": [{"value": 95}, {"value": 100}]}
    if fn == "adjust_pvalues_bonferroni":
        return {"p_pvalues": [0.01, 0.03, 0.05], "p_alpha": 0.05}
    if fn == "calculate_lag":
        return {**base, "p_lag_periods": 1}
    if fn == "calculate_lead":
        return {**base, "p_lead_periods": 1}
    if fn == "analyze_variance":
        return {**base, "p_window_size": 3, "p_group_by": ""}
    if fn == "analyze_distribution":
        return {"p_data": [{"value": 100}, {"value": 110}, {"value": 95}]}
    if fn == "calculate_difference":
        return {**base, "p_order": 1}
    if fn in ("calculate_cdf", "test_stationarity"):
        return base
    if fn == "calculate_rolling_window":
        return {**base, "p_window_size": 2, "p_aggregation": "mean"}
    if fn == "calculate_autocorrelation":
        return {**base, "p_max_lag": 3}
    if fn == "calculate_cumulative":
        return {**base, "p_operation": "sum"}
    if fn == "calculate_percent_change":
        return {**base, "p_periods": 1, "p_method": "simple"}
    if fn == "aggregate_by_time":
        return {
            "p_data": json.loads(
                '[{"time":"2024-01-01 00:00:00","metric":100},{"time":"2024-01-01 06:00:00","metric":150},'
                '{"time":"2024-01-02 00:00:00","metric":120}]'
            ),
            "p_time_column": "time",
            "p_metric_column": "metric",
            "p_period": "day",
            "p_aggregation": "sum",
        }
    if fn in (
        "calculate_moving_average",
        "calculate_growth_rates",
        "calculate_statistical_trend",
        "forecast_linear",
        "calculate_volatility",
        "compare_periods",
        "detect_seasonality",
        "detect_anomalies",
        "calculate_cumulative_trend",
        "classify_trend",
    ):
        if fn == "calculate_moving_average":
            return {**mbase, "p_window_size": 2, "p_ma_type": "simple"}
        if fn == "forecast_linear":
            return {**mbase, "p_periods_ahead": 3}
        if fn == "calculate_volatility":
            return {**mbase, "p_window_size": 2}
        if fn == "detect_anomalies":
            return {**mbase, "p_threshold_std": 2.0, "p_method": "zscore"}
        if fn == "calculate_cumulative_trend":
            return {**mbase, "p_cumulative_type": "sum"}
        return dict(mbase)
    if fn == "get_top_metrics":
        return {
            "p_metrics_data": {
                "revenue": [{"time": "2024-01-01", "value": 100}, {"time": "2024-01-02", "value": 110}],
                "costs": [{"time": "2024-01-01", "value": 50}, {"time": "2024-01-02", "value": 52}],
            },
            "p_n": 5,
            "p_ranking_criteria": "growth",
        }
    return base


def direct_sql(fn: str) -> str | None:
    t = esc_sql_str(TS)
    if fn == "calculate_sma":
        return f"SELECT * FROM unnest(calculate_sma('{t}', 2, ''));"
    if fn == "calculate_wma":
        return f"SELECT * FROM unnest(calculate_wma('{t}', 2, ''));"
    if fn == "calculate_moving_variance":
        return f"SELECT * FROM unnest(calculate_moving_variance('{t}', 2, ''));"
    if fn == "calculate_moving_quantiles":
        return f"SELECT * FROM unnest(calculate_moving_quantiles('{t}', 3, [0.25, 0.5, 0.75]::DOUBLE[], ''));"
    if fn == "calculate_moving_minmax":
        return f"SELECT * FROM unnest(calculate_moving_minmax('{t}', 2, ''));"
    if fn == "calculate_moving_correlation":
        return f"SELECT * FROM unnest(calculate_moving_correlation('{t}', '{t}', 2));"
    if fn == "calculate_moving_sum":
        return f"SELECT * FROM unnest(calculate_moving_sum('{t}', 2, ''));"
    if fn == "calculate_expanding_window":
        return f"SELECT * FROM unnest(calculate_expanding_window('{t}', 'mean', ''));"
    if fn == "calculate_cumulative_operations":
        return f"SELECT * FROM unnest(calculate_cumulative_operations('{t}', '[\"sum\",\"product\",\"max\",\"min\"]', ''));"
    if fn == "calculate_time_weighted_ma":
        return f"SELECT * FROM unnest(calculate_time_weighted_ma('{t}', 0.1, 30));"
    if fn == "calculate_bollinger_bands":
        return f"SELECT * FROM unnest(calculate_bollinger_bands('{t}', 2, 2.0));"
    if fn == "calculate_moving_rank":
        return f"SELECT * FROM unnest(calculate_moving_rank('{t}', 2, ''));"
    if fn == "calculate_ema":
        return f"SELECT * FROM unnest(calculate_ema('{t}', 0.3, ''));"
    return None


def main() -> None:
    fr = json.loads(CAT.read_text())["function_reference"]
    names = sorted(fr.keys())
    lines: list[str] = []
    lines.append("# DuckDB SQL examples — nexcraft-jobs ML / analytics UDFs")
    lines.append("")
    lines.append(
        "Examples for every entry in "
        "`packages/nexcraft-jobs/nexcraft_jobs/compute/udfs/data/sql_functions.json` → `function_reference`. "
        "Assumes `register_analytical_udfs(con)` has been called on the DuckDB connection."
    )
    lines.append("")
    lines.append("## Register UDFs")
    lines.append("")
    lines.append("```python")
    lines.append("import duckdb")
    lines.append("from nexcraft_jobs.compute.udfs import register_analytical_udfs")
    lines.append("")
    lines.append('con = duckdb.connect(":memory:")')
    lines.append("register_analytical_udfs(con)")
    lines.append("```")
    lines.append("")
    lines.append("## Conventions")
    lines.append("")
    lines.append("- **`invoke_sql_function(function_name, payload)`** — `payload` is a single JSON object (string). Result is **`VARCHAR`** JSON; parse with DuckDB `json_extract` / `from_json` as needed.")
    lines.append("- **Direct window UDFs** — `p_data` is a **`VARCHAR`** holding a JSON array of points. Most return **`STRUCT(...)[]`**; use **`SELECT * FROM unnest(...)`** to expand rows.")
    lines.append("- **Helpers** — build `p_data` JSON from grouped arrays:")
    lines.append("")
    lines.append("```sql")
    lines.append("SELECT list_timeseries_to_json(")
    lines.append("  [TIMESTAMP '2024-01-01', TIMESTAMP '2024-01-02']::TIMESTAMP[],")
    lines.append("  [100.0, 105.0]::DOUBLE[]")
    lines.append(");")
    lines.append("")
    lines.append("SELECT list_metric_series_to_json(")
    lines.append("  [TIMESTAMP '2024-01-01', TIMESTAMP '2024-01-02']::TIMESTAMP[],")
    lines.append("  [100.0, 110.0]::DOUBLE[]")
    lines.append(");")
    lines.append("```")
    lines.append("")
    lines.append("Runnable script: `packages/nexcraft-jobs/examples/udfs_fake_postgres_pipeline.py`.")
    lines.append("")
    lines.append("- **`build_anomaly_explanation_payload`** return JSON uses **`segment_breakdown`** (not `impact_by_segment`) for per-segment rows.")
    lines.append("")
    lines.append("## Non-catalog UDFs (also registered)")
    lines.append("")
    lines.append("| UDF | Example |")
    lines.append("|-----|---------|")
    lines.append(r"| `ema` | `SELECT ema([1.0, 2.0, 3.0]::DOUBLE[], 0.5);` |")
    lines.append(r"| `ml_funnel_json` | `SELECT ml_funnel_json('[{\"e\":\"a\",\"u\":1}]', 'e', 'u', '[\"a\",\"b\"]');` |")
    lines.append(r"| `ml_cohort_retention_json` | See `mltools_json.py` for argument shapes. |")
    lines.append(r"| `ml_metrics_summary_json` | `SELECT ml_metrics_summary_json('{}');` |")
    lines.append(r"| `ml_segment_kmeans_json` | `SELECT ml_segment_kmeans_json('[{\"x\":1,\"y\":2}]', 2, 'x,y');` |")
    lines.append(r"| `ml_trend_linear_json` | `SELECT ml_trend_linear_json('[{\"time\":\"2024-01-01\",\"value\":1}]');` |")
    lines.append(r"| `stl_decompose` | Optional if `statsmodels` installed: `SELECT stl_decompose([1.0,2.0,3.0,4.0]::DOUBLE[], 7);` |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Catalog functions")
    lines.append("")

    for fn in names:
        meta = fr[fn]
        lines.append(f"### `{fn}`")
        lines.append("")
        desc = (meta.get("description") or "").strip()
        if desc:
            lines.append(desc)
            lines.append("")
        params = meta.get("parameters") or []
        if params:
            lines.append("**Catalog parameters:**")
            for p in params:
                lines.append(f"- {p}")
            lines.append("")
        pl = payload_for(fn)
        pj = json.dumps(pl, separators=(",", ":"))
        lines.append("**Invoke (returns JSON string):**")
        lines.append("")
        lines.append("```sql")
        lines.append(f"SELECT invoke_sql_function('{fn}', '{esc_sql_str(pj)}');")
        lines.append("```")
        lines.append("")
        dsql = direct_sql(fn)
        if dsql:
            lines.append("**Direct DuckDB UDF (when registered under the same name):**")
            lines.append("")
            lines.append("```sql")
            lines.append(dsql)
            lines.append("```")
            lines.append("")

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {OUT} ({len(names)} functions)")


if __name__ == "__main__":
    main()
