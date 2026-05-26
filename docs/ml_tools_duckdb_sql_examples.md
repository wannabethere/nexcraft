# DuckDB SQL examples — nexcraft-jobs ML / analytics UDFs

Examples for every entry in `packages/nexcraft-jobs/nexcraft_jobs/compute/udfs/data/sql_functions.json` → `function_reference`. Assumes `register_analytical_udfs(con)` has been called on the DuckDB connection.

## Register UDFs

```python
import duckdb
from nexcraft_jobs.compute.udfs import register_analytical_udfs

con = duckdb.connect(":memory:")
register_analytical_udfs(con)
```

## Conventions

- **`invoke_sql_function(function_name, payload)`** — `payload` is a single JSON object (string). Result is **`VARCHAR`** JSON; parse with DuckDB `json_extract` / `from_json` as needed.
- **Direct window UDFs** — `p_data` is a **`VARCHAR`** holding a JSON array of points. Most return **`STRUCT(...)[]`**; use **`SELECT * FROM unnest(...)`** to expand rows.
- **Helpers** — build `p_data` JSON from grouped arrays:

```sql
SELECT list_timeseries_to_json(
  [TIMESTAMP '2024-01-01', TIMESTAMP '2024-01-02']::TIMESTAMP[],
  [100.0, 105.0]::DOUBLE[]
);

SELECT list_metric_series_to_json(
  [TIMESTAMP '2024-01-01', TIMESTAMP '2024-01-02']::TIMESTAMP[],
  [100.0, 110.0]::DOUBLE[]
);
```

Runnable script: `packages/nexcraft-jobs/examples/udfs_fake_postgres_pipeline.py`.

- **`build_anomaly_explanation_payload`** return JSON uses **`segment_breakdown`** (not `impact_by_segment`) for per-segment rows.

## Non-catalog UDFs (also registered)

| UDF | Example |
|-----|---------|
| `ema` | `SELECT ema([1.0, 2.0, 3.0]::DOUBLE[], 0.5);` |
| `ml_funnel_json` | `SELECT ml_funnel_json('[{\"e\":\"a\",\"u\":1}]', 'e', 'u', '[\"a\",\"b\"]');` |
| `ml_cohort_retention_json` | See `mltools_json.py` for argument shapes. |
| `ml_metrics_summary_json` | `SELECT ml_metrics_summary_json('{}');` |
| `ml_segment_kmeans_json` | `SELECT ml_segment_kmeans_json('[{\"x\":1,\"y\":2}]', 2, 'x,y');` |
| `ml_trend_linear_json` | `SELECT ml_trend_linear_json('[{\"time\":\"2024-01-01\",\"value\":1}]');` |
| `stl_decompose` | Optional if `statsmodels` installed: `SELECT stl_decompose([1.0,2.0,3.0,4.0]::DOUBLE[], 7);` |

---

## Catalog functions

### `adjust_pvalues_bonferroni`

Bonferroni correction for multiple comparison adjustment. Prevents false positives in multiple hypothesis testing.

**Catalog parameters:**
- p_pvalues: Array of p-values (DECIMAL[])
- p_alpha: Alpha level (DECIMAL, default 0.05)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('adjust_pvalues_bonferroni', '{"p_pvalues":[0.01,0.03,0.05],"p_alpha":0.05}');
```

### `aggregate_by_time`

Aggregate time series data into time periods (hour, day, week, month, quarter, year). Supports sum, avg, min, max, count, stddev.

**Catalog parameters:**
- p_data: JSONB array of {time, metric}
- p_time_column: Time column (default time)
- p_metric_column: Metric column (default metric)
- p_period: hour|day|week|month|quarter|year (default day)
- p_aggregation: sum|avg|min|max|count|stddev (default sum)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('aggregate_by_time', '{"p_data":[{"time":"2024-01-01 00:00:00","metric":100},{"time":"2024-01-01 06:00:00","metric":150},{"time":"2024-01-02 00:00:00","metric":120}],"p_time_column":"time","p_metric_column":"metric","p_period":"day","p_aggregation":"sum"}');
```

### `analyze_distribution`

Comprehensive distribution analysis with quartiles, skewness, and kurtosis. Supports grouped analysis.

**Catalog parameters:**
- p_data: JSONB array of {value, group?}
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('analyze_distribution', '{"p_data":[{"value":100},{"value":110},{"value":95}]}');
```

### `analyze_variance`

Analyze variance using rolling, expanding, or exponential windows. Returns variance, std dev, CV, and Z-scores.

**Catalog parameters:**
- p_data: JSONB array
- p_window_type: rolling|expanding|exponential (default rolling)
- p_window_size: Window size (INTEGER, default 5)
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('analyze_variance', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_window_size":3,"p_group_by":""}');
```

### `build_anomaly_explanation_payload`

Assembles complete structured JSONB payload for a detected anomaly including stats, correlated metrics, leading indicators (lag analysis), and dimensional decomposition for LLM explanation layer.

**Catalog parameters:**
- p_primary_metric: Primary metric (TEXT)
- p_anomaly_date: Anomaly date (DATE)
- p_lookback_days: Lookback days (INTEGER, default 14)
- p_region: Region filter (TEXT, optional)
- p_product_tier: Product tier filter (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('build_anomaly_explanation_payload', '{"p_primary_metric":"revenue","p_anomaly_date":"2024-01-15","panel":[{"metric_date":"2024-01-01","metric_name":"revenue","metric_value":10.0,"region":"east"},{"metric_date":"2024-01-02","metric_name":"latency","metric_value":50.0,"region":"east"},{"metric_date":"2024-01-01","metric_name":"revenue","metric_value":12.0,"region":"west"}]}');
```

### `calculate_absolute_change_comparison`

Calculate absolute change with standard errors and z-scores for statistical significance testing.

**Catalog parameters:**
- p_data: JSONB array
- p_condition_column: Condition column
- p_baseline_value: Baseline value

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_absolute_change_comparison', '{"p_data":[{"condition":"control","metric":"conversion","value":0.1},{"condition":"treatment","metric":"conversion","value":0.12}],"p_condition_column":"condition","p_baseline_value":"control"}');
```

### `calculate_autocorrelation`

Calculate autocorrelation function (ACF) up to specified lag. Tests significance using confidence bounds.

**Catalog parameters:**
- p_data: JSONB array
- p_max_lag: Maximum lag (INTEGER, default 10)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_autocorrelation', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_max_lag":3}');
```

### `calculate_bollinger_bands`

Calculate Bollinger Bands: SMA with ±N standard deviation bands. Returns bandwidth and %B (position within bands).

**Catalog parameters:**
- p_data: JSONB array
- p_window_size: Window size (INTEGER, default 20)
- p_num_std: Number of std devs (DECIMAL, default 2.0)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_bollinger_bands', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_window_size":2,"p_num_std":2.0}');
```

**Direct DuckDB UDF (when registered under the same name):**

```sql
SELECT * FROM unnest(calculate_bollinger_bands('[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}]', 2, 2.0));
```

### `calculate_bootstrap_ci`

Bootstrap confidence intervals for robust inference. Supports mean, median, and standard deviation.

**Catalog parameters:**
- p_data: JSONB array
- p_metric: mean|median|std (default mean)
- p_confidence_level: Confidence level (DECIMAL, default 95)
- p_bootstrap_samples: Number of samples (INTEGER, default 1000)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_bootstrap_ci', '{"p_data":[{"value":100},{"value":105},{"value":110}],"p_metric":"mean","p_confidence_level":95,"p_bootstrap_samples":200}');
```

### `calculate_cdf`

Calculate empirical cumulative distribution function. Returns percentile ranks and cumulative probabilities.

**Catalog parameters:**
- p_data: JSONB array
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_cdf', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}]}');
```

### `calculate_cumulative`

Calculate cumulative operations: sum, product, max, min. Returns percent of total for cumulative sum. Input uses 'value' field.

**Catalog parameters:**
- p_data: JSONB array
- p_operation: sum|product|max|min (default sum)
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_cumulative', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_operation":"sum"}');
```

### `calculate_cumulative_operations`

Calculate multiple cumulative operations: sum, product, max, min. Returns percent_of_total for cumulative sum.

**Catalog parameters:**
- p_data: JSONB array
- p_operations: TEXT array (default ['sum','product','max','min'])
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_cumulative_operations', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_operations":["sum","product","max","min"],"p_group_by":""}');
```

**Direct DuckDB UDF (when registered under the same name):**

```sql
SELECT * FROM unnest(calculate_cumulative_operations('[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}]', '["sum","product","max","min"]', ''));
```

### `calculate_cumulative_trend`

Calculate cumulative values over time (sum, avg, max, min). From trend_analysis_functions - input uses 'metric' field. Returns cumulative percentages. Note: Same name as timeseries calculate_cumulative which uses 'value' field.

**Catalog parameters:**
- p_data: JSONB array of {time, metric}
- p_cumulative_type: sum|avg|max|min (default sum)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_cumulative_trend', '{"p_data":[{"time":"2024-01-01T00:00:00","metric":100.0},{"time":"2024-01-02T00:00:00","metric":110.0},{"time":"2024-01-03T00:00:00","metric":105.0}],"p_cumulative_type":"sum"}');
```

### `calculate_cuped_adjustment`

CUPED (Controlled-experiment Using Pre-Experiment Data) adjustment. Reduces variance using pre-experiment covariates.

**Catalog parameters:**
- p_data: JSONB array
- p_treatment_column: Treatment column
- p_pre_metric_column: Pre-metric column (default pre_value)
- p_post_metric_column: Post-metric column (default post_value)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_cuped_adjustment', '{"p_data":[]}');
```

### `calculate_difference`

Calculate first and second-order differences for stationarity. Tests whether differenced series is stationary.

**Catalog parameters:**
- p_data: JSONB array
- p_order: 1 for first difference, 2 for second (INTEGER, default 1)
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_difference', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_order":1}');
```

### `calculate_effect_sizes`

Calculate multiple effect size measures: Cohen d, Hedges g, Glass delta. Provides interpretation (negligible/small/medium/large).

**Catalog parameters:**
- p_data_treatment: JSONB array for treatment group
- p_data_control: JSONB array for control group

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_effect_sizes', '{"p_data_treatment":[{"value":105},{"value":110}],"p_data_control":[{"value":95},{"value":100}]}');
```

### `calculate_ema`

Exponential moving average with configurable smoothing factor. Alpha closer to 1 = more responsive, closer to 0 = smoother.

**Catalog parameters:**
- p_data: JSONB array
- p_alpha: Smoothing factor 0<alpha<=1 (DECIMAL, default 0.3)
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_ema', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_alpha":0.3,"p_group_by":""}');
```

**Direct DuckDB UDF (when registered under the same name):**

```sql
SELECT * FROM unnest(calculate_ema('[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}]', 0.3, ''));
```

### `calculate_expanding_window`

Calculate metrics using all prior data (expanding window). Operations: mean, sum, std, min, max, count.

**Catalog parameters:**
- p_data: JSONB array
- p_operation: mean|sum|std|min|max|count (default mean)
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_expanding_window', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_window_size":2,"p_group_by":""}');
```

**Direct DuckDB UDF (when registered under the same name):**

```sql
SELECT * FROM unnest(calculate_expanding_window('[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}]', 'mean', ''));
```

### `calculate_growth_rates`

Calculate period-over-period growth rates with annualization. Supports period_over_period, year_over_year, compound.

**Catalog parameters:**
- p_data: JSONB array of {time, metric}
- p_period_type: period_over_period|year_over_year|compound (default period_over_period)
- p_periods: Periods (INTEGER, default 1)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_growth_rates', '{"p_data":[{"time":"2024-01-01T00:00:00","metric":100.0},{"time":"2024-01-02T00:00:00","metric":110.0},{"time":"2024-01-03T00:00:00","metric":105.0}]}');
```

### `calculate_lag`

Calculate lagged values (shift backward in time) with change metrics. Supports grouping for panel data.

**Catalog parameters:**
- p_data: JSONB array of {time, value}
- p_lag_periods: Lag periods (INTEGER, default 1)
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_lag', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_lag_periods":1}');
```

### `calculate_lag_correlation`

Sweeps lag -N to +N for a metric pair and computes Pearson correlation at each lag. Negative lag = other_metric LEADS primary (causal candidate). Positive lag = other_metric LAGS primary.

**Catalog parameters:**
- p_primary_metric: Primary metric (TEXT)
- p_other_metric: Other metric (TEXT)
- p_anomaly_date: Anomaly date (DATE)
- p_lookback_days: Lookback days (INTEGER, default 14)
- p_max_lag: Sweep range (INTEGER, default 5)
- p_region: Region filter (TEXT, optional)
- p_product_tier: Product tier filter (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_lag_correlation', '{"p_primary_metric":"revenue","panel":[{"metric_date":"2024-01-01","metric_name":"revenue","metric_value":10.0,"region":"east"},{"metric_date":"2024-01-02","metric_name":"latency","metric_value":50.0,"region":"east"},{"metric_date":"2024-01-01","metric_name":"revenue","metric_value":12.0,"region":"west"}]}');
```

### `calculate_lead`

Calculate lead values (shift forward in time) for predictive analysis. Useful for forward-looking features.

**Catalog parameters:**
- p_data: JSONB array
- p_lead_periods: Lead periods (INTEGER, default 1)
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_lead', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_lead_periods":1}');
```

### `calculate_moving_average`

Calculate moving averages: Simple (SMA), Weighted (WMA), or Exponential (EMA). Input uses 'metric' field. Detects deviations from trend.

**Catalog parameters:**
- p_data: JSONB array of {time, metric}
- p_window_size: Window size (INTEGER, default 7)
- p_ma_type: simple|weighted|exponential (default simple)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_moving_average', '{"p_data":[{"time":"2024-01-01T00:00:00","metric":100.0},{"time":"2024-01-02T00:00:00","metric":110.0},{"time":"2024-01-03T00:00:00","metric":105.0}],"p_window_size":2,"p_ma_type":"simple"}');
```

### `calculate_moving_correlation`

Calculate moving correlation between two time series. Identifies changing relationships over time.

**Catalog parameters:**
- p_data_x: JSONB array for series X
- p_data_y: JSONB array for series Y
- p_window_size: Window size (INTEGER, default 7)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_moving_correlation', '{"p_data_x":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_data_y":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_window_size":2}');
```

**Direct DuckDB UDF (when registered under the same name):**

```sql
SELECT * FROM unnest(calculate_moving_correlation('[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}]', '[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}]', 2));
```

### `calculate_moving_minmax`

Calculate moving min/max with range and position metrics. position_in_range: 0=at min, 1=at max.

**Catalog parameters:**
- p_data: JSONB array
- p_window_size: Window size (INTEGER, default 7)
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_moving_minmax', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_window_size":2,"p_group_by":""}');
```

**Direct DuckDB UDF (when registered under the same name):**

```sql
SELECT * FROM unnest(calculate_moving_minmax('[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}]', 2, ''));
```

### `calculate_moving_quantiles`

Calculate moving quartiles (Q1, median, Q3) and IQR. Robust to outliers.

**Catalog parameters:**
- p_data: JSONB array
- p_window_size: Window size (INTEGER, default 7)
- p_quantiles: Quantile array (DECIMAL[], default [0.25,0.5,0.75])
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_moving_quantiles', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_window_size":3,"p_quantiles":[0.25,0.5,0.75],"p_group_by":""}');
```

**Direct DuckDB UDF (when registered under the same name):**

```sql
SELECT * FROM unnest(calculate_moving_quantiles('[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}]', 3, [0.25, 0.5, 0.75]::DOUBLE[], ''));
```

### `calculate_moving_rank`

Calculate moving rank and percentile rank within window. Identifies relative position of values.

**Catalog parameters:**
- p_data: JSONB array
- p_window_size: Window size (INTEGER, default 7)
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_moving_rank', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_window_size":2,"p_group_by":""}');
```

**Direct DuckDB UDF (when registered under the same name):**

```sql
SELECT * FROM unnest(calculate_moving_rank('[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}]', 2, ''));
```

### `calculate_moving_sum`

Calculate moving sum with contribution percentage. Shows each value's contribution to recent total.

**Catalog parameters:**
- p_data: JSONB array
- p_window_size: Window size (INTEGER, default 7)
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_moving_sum', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_window_size":2,"p_group_by":""}');
```

**Direct DuckDB UDF (when registered under the same name):**

```sql
SELECT * FROM unnest(calculate_moving_sum('[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}]', 2, ''));
```

### `calculate_moving_variance`

Calculate moving variance, std dev, coefficient of variation, and Z-scores for volatility analysis.

**Catalog parameters:**
- p_data: JSONB array
- p_window_size: Window size (INTEGER, default 7)
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_moving_variance', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_window_size":2,"p_group_by":""}');
```

**Direct DuckDB UDF (when registered under the same name):**

```sql
SELECT * FROM unnest(calculate_moving_variance('[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}]', 2, ''));
```

### `calculate_percent_change`

Calculate period-over-period percent changes. Supports simple, log, and compound methods. Auto-categorizes magnitude.

**Catalog parameters:**
- p_data: JSONB array
- p_periods: Periods back (INTEGER, default 1)
- p_method: simple|log|compound (default simple)
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_percent_change', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_periods":1,"p_method":"simple"}');
```

### `calculate_percent_change_comparison`

Calculate percent change between treatment and baseline groups. Returns relative uplift and absolute change for A/B test analysis.

**Catalog parameters:**
- p_data: JSONB array
- p_condition_column: Condition column name
- p_baseline_value: Baseline value
- p_metric_columns: Metric columns (TEXT[], default ['value'])

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_percent_change_comparison', '{"p_data":[{"condition":"control","metric":"conversion","value":0.1},{"condition":"treatment","metric":"conversion","value":0.12}],"p_condition_column":"condition","p_baseline_value":"control"}');
```

### `calculate_power_analysis`

Sample size calculation for desired statistical power. Returns required sample size per group and Cohen d.

**Catalog parameters:**
- p_effect_size: Effect size (DECIMAL)
- p_baseline_std: Baseline standard deviation (DECIMAL)
- p_alpha: Alpha level (DECIMAL, default 0.05)
- p_power: Target power (DECIMAL, default 0.80)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_power_analysis', '{"p_effect_size":10.0,"p_baseline_std":25.0,"p_alpha":0.05,"p_power":0.8}');
```

### `calculate_prepost_comparison`

Pre-post analysis for within-subject comparisons. Automatically determines cutoff time if not provided.

**Catalog parameters:**
- p_data: JSONB array
- p_entity_id_column: Entity column (default entity_id)
- p_time_column: Time column (default time)
- p_cutoff_time: Cutoff timestamp (optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_prepost_comparison', '{"p_data":[]}');
```

### `calculate_rolling_window`

General-purpose rolling window with multiple aggregations. Supports mean, sum, min, max, std, count.

**Catalog parameters:**
- p_data: JSONB array
- p_window_size: Window size (INTEGER, default 5)
- p_aggregation: mean|sum|min|max|std|count (default mean)
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_rolling_window', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_window_size":2,"p_aggregation":"mean"}');
```

### `calculate_sequential_analysis`

Sequential analysis for A/B test monitoring with early stopping. Implements O'Brien-Fleming boundary (simplified).

**Catalog parameters:**
- p_data: JSONB array
- p_treatment_column: Treatment column
- p_treatment_value: Treatment value
- p_control_value: Control value
- p_alpha: Alpha (DECIMAL, default 0.05)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_sequential_analysis', '{"p_data":[]}');
```

### `calculate_sma`

Calculate Simple Moving Average with Bollinger-style bands. Returns SMA, deviation, and upper/lower bands (±2 std dev).

**Catalog parameters:**
- p_data: JSONB array of {time, value, group?}
- p_window_size: Window size (INTEGER, default 7)
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_sma', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_window_size":2,"p_group_by":""}');
```

**Direct DuckDB UDF (when registered under the same name):**

```sql
SELECT * FROM unnest(calculate_sma('[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}]', 2, ''));
```

### `calculate_statistical_trend`

Perform linear regression to identify statistical trends. Returns slope, R-squared, correlation, and significance testing.

**Catalog parameters:**
- p_data: JSONB array of {time, metric}
- p_confidence_level: Confidence level (DECIMAL, default 95)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_statistical_trend', '{"p_data":[{"time":"2024-01-01T00:00:00","metric":100.0},{"time":"2024-01-02T00:00:00","metric":110.0},{"time":"2024-01-03T00:00:00","metric":105.0}]}');
```

### `calculate_stratified_analysis`

Stratified analysis (Mantel-Haenszel style) for confounding adjustment. Calculates stratum-specific effects with weights.

**Catalog parameters:**
- p_data: JSONB array
- p_treatment_column: Treatment column
- p_treatment_value: Treatment value
- p_control_value: Control value
- p_strata_column: Strata column

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_stratified_analysis', '{"p_data":[]}');
```

### `calculate_time_weighted_ma`

Calculate time-weighted moving average with exponential decay. More weight on recent observations.

**Catalog parameters:**
- p_data: JSONB array
- p_decay_factor: Decay factor (DECIMAL, default 0.1)
- p_window_size: Window size (INTEGER, default 30)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_time_weighted_ma', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_decay_factor":0.1,"p_window_size":30}');
```

**Direct DuckDB UDF (when registered under the same name):**

```sql
SELECT * FROM unnest(calculate_time_weighted_ma('[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}]', 0.1, 30));
```

### `calculate_volatility`

Calculate rolling volatility metrics including std dev and coefficient of variation. Classifies: very_low, low, moderate, high, very_high.

**Catalog parameters:**
- p_data: JSONB array of {time, metric}
- p_window_size: Window size (INTEGER, default 30)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_volatility', '{"p_data":[{"time":"2024-01-01T00:00:00","metric":100.0},{"time":"2024-01-02T00:00:00","metric":110.0},{"time":"2024-01-03T00:00:00","metric":105.0}],"p_window_size":2}');
```

### `calculate_wma`

Calculate Weighted Moving Average giving more weight to recent values. More responsive than SMA.

**Catalog parameters:**
- p_data: JSONB array of {time, value}
- p_window_size: Window size (INTEGER, default 7)
- p_group_by: Group column (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('calculate_wma', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}],"p_window_size":2,"p_group_by":""}');
```

**Direct DuckDB UDF (when registered under the same name):**

```sql
SELECT * FROM unnest(calculate_wma('[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}]', 2, ''));
```

### `classify_trend`

Comprehensive trend classification with velocity and acceleration. Provides actionable recommendations.

**Catalog parameters:**
- p_data: JSONB array of {time, metric}

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('classify_trend', '{"p_data":[{"time":"2024-01-01T00:00:00","metric":100.0},{"time":"2024-01-02T00:00:00","metric":110.0},{"time":"2024-01-03T00:00:00","metric":105.0}]}');
```

### `compare_periods`

Compare current period values with previous periods. Useful for MoM, QoQ, YoY analysis.

**Catalog parameters:**
- p_data: JSONB array of {time, metric}
- p_comparison_type: previous|year_ago|quarter_ago (default previous)
- p_n_periods: Periods back (INTEGER, default 1)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('compare_periods', '{"p_data":[{"time":"2024-01-01T00:00:00","metric":100.0},{"time":"2024-01-02T00:00:00","metric":110.0},{"time":"2024-01-03T00:00:00","metric":105.0}]}');
```

### `decompose_impact_by_dimension`

Breaks total anomaly impact into segment-level contributions. Compares anomaly period values against baseline average. contribution_to_total shows each segment as % of total delta.

**Catalog parameters:**
- p_metric_name: Metric name (TEXT)
- p_anomaly_date: Anomaly date (DATE)
- p_dimension: Dimension type - 'region', 'product_tier', 'region_tier' (TEXT)
- p_baseline_days: Days before anomaly for baseline (INTEGER, default 7)
- p_comparison_days: Anomaly window width (INTEGER, default 1)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('decompose_impact_by_dimension', '{"p_metric_name":"revenue","p_dimension":"region","panel":[{"metric_date":"2024-01-01","metric_name":"revenue","metric_value":10.0,"region":"east"},{"metric_date":"2024-01-02","metric_name":"latency","metric_value":50.0,"region":"east"},{"metric_date":"2024-01-01","metric_name":"revenue","metric_value":12.0,"region":"west"}]}');
```

### `detect_anomalies`

Detect statistical anomalies using Z-score or IQR methods. Flags high and low outliers with anomaly scores.

**Catalog parameters:**
- p_data: JSONB array of {time, metric}
- p_threshold_std: Std devs from mean (DECIMAL, default 2.0)
- p_method: zscore|iqr|mad (default zscore)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('detect_anomalies', '{"p_data":[{"time":"2024-01-01T00:00:00","metric":100.0},{"time":"2024-01-02T00:00:00","metric":110.0},{"time":"2024-01-03T00:00:00","metric":105.0}],"p_threshold_std":2.0,"p_method":"zscore"}');
```

### `detect_seasonality`

Detect seasonal patterns by grouping data into seasonal periods. Returns seasonal indices showing deviation from average.

**Catalog parameters:**
- p_data: JSONB array of {time, metric}
- p_season_length: Season length e.g. 12 for monthly yearly (INTEGER, default 12)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('detect_seasonality', '{"p_data":[{"time":"2024-01-01T00:00:00","metric":100.0},{"time":"2024-01-02T00:00:00","metric":110.0},{"time":"2024-01-03T00:00:00","metric":105.0}]}');
```

### `find_correlated_metrics`

Scans all metrics and ranks by Pearson correlation strength against the primary metric within a lookback window ending at the anomaly date. Answers: 'Which other metrics moved with the anomaly?'

**Catalog parameters:**
- p_primary_metric: Primary metric name (TEXT)
- p_anomaly_date: Anomaly date (DATE)
- p_lookback_days: Lookback window days (INTEGER, default 14)
- p_min_correlation: Filter weak correlations (DECIMAL, default 0.60)
- p_region: Region filter (TEXT, optional)
- p_product_tier: Product tier filter (TEXT, optional)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('find_correlated_metrics', '{"p_primary_metric":"revenue","panel":[{"metric_date":"2024-01-01","metric_name":"revenue","metric_value":10.0,"region":"east"},{"metric_date":"2024-01-02","metric_name":"latency","metric_value":50.0,"region":"east"},{"metric_date":"2024-01-01","metric_name":"revenue","metric_value":12.0,"region":"west"}],"p_min_correlation":0.5}');
```

### `forecast_linear`

Generate linear forecasts with confidence intervals. Uses trend analysis to project future values.

**Catalog parameters:**
- p_data: JSONB array of {time, metric}
- p_periods_ahead: Forecast periods (INTEGER, default 7)
- p_confidence_interval: Confidence level (DECIMAL, default 95)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('forecast_linear', '{"p_data":[{"time":"2024-01-01T00:00:00","metric":100.0},{"time":"2024-01-02T00:00:00","metric":110.0},{"time":"2024-01-03T00:00:00","metric":105.0}],"p_periods_ahead":3}');
```

### `get_top_metrics`

Rank metrics by criteria: growth, volatility, absolute_value, trend_strength. Returns top N metrics.

**Catalog parameters:**
- p_metrics_data: JSONB object with metric_name keys and arrays of {time, value}
- p_n: Top N (INTEGER, default 5)
- p_ranking_criteria: growth|volatility|absolute_value|trend_strength (default growth)

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('get_top_metrics', '{"p_metrics_data":{"revenue":[{"time":"2024-01-01","value":100},{"time":"2024-01-02","value":110}],"costs":[{"time":"2024-01-01","value":50},{"time":"2024-01-02","value":52}]},"p_n":5,"p_ranking_criteria":"growth"}');
```

### `test_stationarity`

Simplified stationarity test checking mean, variance, and trend stability. Returns actionable recommendations.

**Catalog parameters:**
- p_data: JSONB array

**Invoke (returns JSON string):**

```sql
SELECT invoke_sql_function('test_stationarity', '{"p_data":[{"time":"2024-01-01T00:00:00","value":100.0},{"time":"2024-01-02T00:00:00","value":105.0},{"time":"2024-01-03T00:00:00","value":102.0}]}');
```

