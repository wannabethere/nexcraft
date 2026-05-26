# Jobs 03 — DuckDB Compute and Analytical UDFs

The compute phase runs on DuckDB. This document specifies the connection setup, what UDFs ship in the box, and the authoring pattern for SQL-first analytical recipes.

## Why DuckDB

The right primitive for this layer:

- **Arrow-native zero-copy.** `con.register("name", record_batch_reader)` exposes a stream as a DuckDB table without materialization or copy.
- **Spills to disk.** With `memory_limit` and `temp_directory` set, DuckDB transparently spills hash tables, sorts, and aggregates that don't fit in memory. This is the TB-scale safety net.
- **Vectorized execution.** Real C++ engine. Performance comparable to ClickHouse/Snowflake for in-process workloads.
- **Window functions and statistical aggregates are first-class.** `regr_slope`, `stddev_samp`, `time_bucket`, full SQL standard window frames including `RANGE BETWEEN INTERVAL '7 days' PRECEDING`.
- **Python UDFs receive Arrow batches, not rows.** With `type="arrow"`, NumPy/SciPy operations stay zero-copy.

## Connection setup

The runtime constructs the DuckDB connection per job, with budgets from `JobContext`:

```python
import duckdb

def setup_duckdb(ctx: JobContext) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(f"SET memory_limit = '{ctx.memory_budget}'")
    con.execute(f"SET threads = {ctx.cpu_budget}")
    if ctx.scratch_dir:
        con.execute(f"SET temp_directory = '{ctx.scratch_dir}'")
    # Enable disk spilling explicitly
    con.execute("SET preserve_insertion_order = false")  # allows out-of-core hash joins
    return con
```

`memory_limit` is a hard cap. When exceeded, DuckDB spills to `temp_directory`. The recipe's `memory_budget` is sized to fit the working set; spilling is a graceful fallback, not the primary path.

### Stream registration

Recipe inputs (Parquet on object storage from extract phase, or `RecordBatchReader` in `LocalRuntime`) are registered before `compute()` runs:

```python
# Temporal path — read Parquet from object storage
for name, dataset in extract_results.datasets.items():
    con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{dataset.storage_uri}')")

# Local path — direct Arrow stream
for name, reader in streams.items():
    con.register(name, reader)
```

Recipes write SQL against these table names. Authoring is uniform regardless of runtime.

## The analytical UDF library

Pre-registered on every recipe's connection. Lives in `nexcraft_jobs.compute.udfs`. All UDFs use `type="arrow"` for vectorized execution.

### Time series

```python
# nexcraft_jobs/compute/udfs/timeseries.py
import pyarrow as pa
import numpy as np
from scipy import signal

def detrend_arrow(values: pa.ListArray) -> pa.ListArray:
    """Linear detrend per group. Removes ax+b trend."""
    out = []
    for v in values:
        arr = np.asarray(v.values)
        out.append(signal.detrend(arr, type="linear"))
    return pa.array(out, type=pa.list_(pa.float64()))

def stl_decompose(values: pa.ListArray, period: pa.Int32Scalar) -> pa.StructArray:
    """STL decomposition: trend + seasonal + residual.
    Args:
        values: list<double> — series per group
        period: int — seasonal period
    Returns: struct<trend list<double>, seasonal list<double>, resid list<double>>
    """
    from statsmodels.tsa.seasonal import STL
    p = period.as_py()
    trends, seasons, resids = [], [], []
    for v in values:
        res = STL(np.asarray(v.values), period=p).fit()
        trends.append(list(res.trend))
        seasons.append(list(res.seasonal))
        resids.append(list(res.resid))
    return pa.StructArray.from_arrays(
        [pa.array(trends), pa.array(seasons), pa.array(resids)],
        names=["trend", "seasonal", "resid"],
    )

def ema(values: pa.ListArray, alpha: pa.DoubleScalar) -> pa.ListArray:
    """Exponential moving average."""
    a = alpha.as_py()
    out = []
    for v in values:
        arr = np.asarray(v.values)
        e = np.empty_like(arr)
        e[0] = arr[0]
        for i in range(1, len(arr)):
            e[i] = a * arr[i] + (1 - a) * e[i-1]
        out.append(e)
    return pa.array(out, type=pa.list_(pa.float64()))
```

### Changepoint detection

```python
# nexcraft_jobs/compute/udfs/changepoints.py
def changepoints_pelt(values: pa.ListArray, penalty: pa.DoubleScalar) -> pa.ListArray:
    """PELT changepoint detection. Returns list of changepoint indices per group."""
    import ruptures as rpt
    pen = penalty.as_py()
    out = []
    for v in values:
        arr = np.asarray(v.values).reshape(-1, 1)
        algo = rpt.Pelt(model="rbf").fit(arr)
        cps = algo.predict(pen=pen)
        out.append(cps[:-1])  # last is always len(arr)
    return pa.array(out, type=pa.list_(pa.int64()))
```

### Anomaly scoring

```python
# nexcraft_jobs/compute/udfs/anomaly.py
def iforest_score(values: pa.ListArray) -> pa.ListArray:
    """Isolation Forest anomaly scores per series. Returns list<double> per group."""
    from sklearn.ensemble import IsolationForest
    out = []
    for v in values:
        arr = np.asarray(v.values).reshape(-1, 1)
        model = IsolationForest(contamination="auto", random_state=42)
        scores = -model.fit(arr).score_samples(arr)  # higher = more anomalous
        out.append(scores)
    return pa.array(out, type=pa.list_(pa.float64()))
```

### Forecasting (optional dep)

```python
# nexcraft_jobs/compute/udfs/forecast.py — install with extras [forecast]
def arima_forecast(values: pa.ListArray, horizon: pa.Int32Scalar) -> pa.ListArray:
    """ARIMA(1,1,1) forecast. Returns list<double> of length `horizon` per group."""
    from statsmodels.tsa.arima.model import ARIMA
    h = horizon.as_py()
    out = []
    for v in values:
        arr = np.asarray(v.values)
        try:
            res = ARIMA(arr, order=(1,1,1)).fit()
            out.append(list(res.forecast(steps=h)))
        except Exception:
            out.append([float("nan")] * h)
    return pa.array(out, type=pa.list_(pa.float64()))
```

### Registration

```python
# nexcraft_jobs/compute/udfs/__init__.py
def register_analytical_udfs(con: duckdb.DuckDBPyConnection) -> None:
    """Registers the full analytical UDF library on the connection."""
    from .timeseries import detrend_arrow, stl_decompose, ema
    from .changepoints import changepoints_pelt
    from .anomaly import iforest_score

    con.create_function("detrend", detrend_arrow,
                        [pa.list_(pa.float64())], pa.list_(pa.float64()),
                        type="arrow")
    con.create_function("stl_decompose", stl_decompose,
                        [pa.list_(pa.float64()), pa.int32()],
                        pa.struct([("trend", pa.list_(pa.float64())),
                                   ("seasonal", pa.list_(pa.float64())),
                                   ("resid", pa.list_(pa.float64()))]),
                        type="arrow")
    con.create_function("ema", ema,
                        [pa.list_(pa.float64()), pa.float64()],
                        pa.list_(pa.float64()), type="arrow")
    con.create_function("changepoints_pelt", changepoints_pelt,
                        [pa.list_(pa.float64()), pa.float64()],
                        pa.list_(pa.int64()), type="arrow")
    con.create_function("iforest_score", iforest_score,
                        [pa.list_(pa.float64())], pa.list_(pa.float64()),
                        type="arrow")
    # Forecast UDFs only if optional deps installed
    try:
        from .forecast import arima_forecast
        con.create_function("arima_forecast", arima_forecast,
                            [pa.list_(pa.float64()), pa.int32()],
                            pa.list_(pa.float64()), type="arrow")
    except ImportError:
        pass
```

## What ships built-in (no UDF needed)

Recipe authors should reach for built-in DuckDB capabilities first. The UDFs are for what these can't do.

### Window functions

```sql
-- 30-day moving average, time-windowed (not row-windowed)
AVG(value) OVER (
  PARTITION BY region
  ORDER BY ts
  RANGE BETWEEN INTERVAL '30 days' PRECEDING AND CURRENT ROW
) AS ma_30d

-- Rank over groups
RANK() OVER (PARTITION BY region ORDER BY value DESC)

-- Lag/lead for variance computation
value - LAG(value, 1) OVER (PARTITION BY region ORDER BY ts) AS delta
```

### Statistical aggregates

```sql
-- Linear regression coefficients per group — for trend lines
SELECT region,
       regr_slope(value, EXTRACT(EPOCH FROM ts))     AS slope,
       regr_intercept(value, EXTRACT(EPOCH FROM ts)) AS intercept,
       regr_r2(value, EXTRACT(EPOCH FROM ts))        AS r_squared
FROM observations GROUP BY region;

-- Standard deviation, variance
stddev_samp(value), var_samp(value)

-- Approximate quantiles for big data
approx_quantile(value, 0.95)
```

### Time bucketing

```sql
-- Timescale-style bucketing into fixed intervals
SELECT time_bucket(INTERVAL '1 hour', ts) AS bucket,
       AVG(value)
FROM observations GROUP BY bucket;
```

### Outlier detection in pure SQL

```sql
-- Z-score based outliers, no UDF
SELECT *,
       (value - AVG(value) OVER w) / NULLIF(STDDEV(value) OVER w, 0) AS z
FROM observations
WINDOW w AS (PARTITION BY region ORDER BY ts ROWS BETWEEN 30 PRECEDING AND CURRENT ROW)
QUALIFY ABS(z) > 3;
```

## SQL macros for reusable patterns

DuckDB `CREATE MACRO` lets recipe libraries share parameterized SQL templates. Macros expand at parse time — zero runtime overhead.

```sql
CREATE MACRO moving_avg(col, ts_col, days) AS (
    AVG(col) OVER (
      ORDER BY ts_col
      RANGE BETWEEN INTERVAL (days || ' days') PRECEDING AND CURRENT ROW
    )
);

-- Used in any recipe:
SELECT region, ts, moving_avg(value, ts, 30) FROM observations;
```

The runtime registers a small standard library of macros alongside the UDFs.

## The authoring pattern

The mapping from analytical task to implementation:

| Task | Approach |
|---|---|
| Simple/weighted moving average | Built-in window function |
| Exponential moving average | `ema()` UDF (simpler than recursive CTE) |
| Linear trend per group | Built-in `regr_*` aggregates |
| STL decomposition | `stl_decompose()` UDF on aggregated lists |
| Changepoint detection | `changepoints_pelt()` UDF |
| Variance decomposition | Pure SQL with CTEs and windows |
| Z-score / IQR outliers | Built-in stats with windows |
| Isolation Forest anomalies | `iforest_score()` UDF |
| ARIMA forecasting | `arima_forecast()` UDF (optional dep) |
| Cohort retention | Pure SQL self-joins |

The pattern: **SQL + windows for the bulk; UDFs only where SQL can't express the math.** This keeps the engine doing what it's good at (vectorized aggregation, joins, partitioning) and reserves Python for the actual statistical method.

## Memory and budget enforcement

DuckDB enforces `memory_limit` automatically — exceeding it triggers spilling, and exceeding both memory and disk raises an `OutOfMemoryException`. The compute activity catches this and translates to a structured `BudgetExceededError`:

```python
try:
    result = con.execute(sql).arrow()
except duckdb.OutOfMemoryException as e:
    raise BudgetExceededError(
        budget_kind="memory",
        limit=ctx.memory_budget,
        observed="exceeded",
    ) from e
```

For row-budget enforcement at compute time (e.g., a `LIMIT 10_000_000` cap on the result), recipes use SQL `LIMIT` directly. The runtime doesn't second-guess.

## Why not pandas

Recipe authors will be tempted to call `con.execute(...).df()` and "just do it in pandas." Discourage this:

- Pandas materializes everything in memory; DuckDB doesn't.
- Pandas operations are single-threaded; DuckDB is parallel.
- pyarrow ↔ pandas conversion costs are real at TB scale.

Document the rule in the recipe authoring guide: **`.arrow()`, never `.df()`**. If a recipe genuinely needs pandas (some `statsmodels` API takes only DataFrames), wrap that call in a UDF over a single group's data, not across the whole result.
