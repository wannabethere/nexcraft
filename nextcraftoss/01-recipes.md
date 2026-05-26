# Jobs 01 — The Recipe Pattern

A recipe is a Python class implementing the four-phase contract: `validate → extract → compute → persist`. Stored in any package, registered in any application. Recipes are the authoring surface; the runtime is what runs them.

## The protocol

```python
from typing import Protocol
from dataclasses import dataclass
import pyarrow as pa
from nexcraft import FedSQLClient
from nexcraft_jobs.context import JobContext
from nexcraft_jobs.store import ResultStore, ResultRef

@dataclass
class ComputeResult:
    """What compute() returns. The shape depends on the recipe."""
    primary: pa.Table              # the headline result
    auxiliaries: dict[str, pa.Table] = None  # optional secondary tables
    metadata: dict = None          # JSON-serializable summary stats

class Recipe(Protocol):
    name: str                      # e.g. "variance_analysis"
    version: str                   # SemVer; recipe code is versioned

    def validate(self, params: dict) -> None:
        """Raises ValueError if params are invalid. Called before scheduling."""

    async def extract(
        self,
        params: dict,
        ctx: JobContext,
        fedsql: FedSQLClient,
    ) -> dict[str, pa.RecordBatchReader]:
        """Pulls source data via nexcraft. Returns named Arrow streams.
        Streams are lazy; not consumed until compute reads them."""

    async def compute(
        self,
        inputs: dict[str, pa.RecordBatchReader],
        params: dict,
        ctx: JobContext,
    ) -> ComputeResult:
        """Heavy lifting. DuckDB connection is set up by runtime
        and the inputs are pre-registered as tables before compute() runs."""

    async def persist(
        self,
        result: ComputeResult,
        params: dict,
        ctx: JobContext,
        store: ResultStore,
    ) -> ResultRef:
        """Writes Parquet + metadata. Returns a ref for callers to resolve."""
```

## `JobContext`

Extends the QueryContext concept with job-specific budgets:

```python
@dataclass(frozen=True)
class JobContext:
    # Identity
    job_id: str
    tenant_id: str
    recipe_name: str
    recipe_version: str
    submitted_at: datetime
    workflow_id: str               # Temporal workflow ID, if applicable

    # Tracing
    trace_id: str | None = None

    # Budgets — propagate into nexcraft QueryContexts during extract
    extract_row_budget: int | None = 50_000_000
    extract_byte_budget: int | None = None
    extract_deadline_per_query: timedelta = timedelta(minutes=10)

    # Compute budgets — passed to DuckDB
    memory_budget: str = "8GB"
    cpu_budget: int = 4
    scratch_dir: Path | None = None    # spill directory; tmpfs or fast SSD

    # Workflow budget — total wall clock for the whole job
    job_deadline: datetime | None = None

    # Cancellation
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
```

The recipe author rarely touches these directly; the runtime threads them through. But the design exposes them so users who need to override (e.g., a `LongRunningTrendRecipe` that legitimately needs 32GB) can do so via the submission API.

## A worked example: variance analysis

Concrete recipe demonstrating the pattern end-to-end.

```python
from nexcraft_jobs.recipe import Recipe, ComputeResult
from nexcraft_jobs.compute.duckdb_helpers import ScopedDuckDBConnection

class VarianceAnalysisRecipe:
    name = "variance_analysis"
    version = "1.0.0"

    def validate(self, params):
        required = ["actuals_source", "forecasts_source", "period_start", "period_end"]
        missing = [k for k in required if k not in params]
        if missing:
            raise ValueError(f"missing params: {missing}")

    async def extract(self, params, ctx, fedsql):
        from nexcraft.core import QueryContext
        actuals_ctx = QueryContext(
            tenant_id=ctx.tenant_id,
            query_id=f"{ctx.job_id}-actuals",
            deadline=datetime.now(UTC) + ctx.extract_deadline_per_query,
            max_rows=ctx.extract_row_budget,
            max_bytes=ctx.extract_byte_budget,
        )
        forecasts_ctx = replace(actuals_ctx, query_id=f"{ctx.job_id}-forecasts")

        actuals_sql = f"""
            SELECT region, period, SUM(amount) AS actual
            FROM sales
            WHERE period BETWEEN '{params['period_start']}' AND '{params['period_end']}'
            GROUP BY region, period
        """
        forecasts_sql = f"""
            SELECT region, period, SUM(amount) AS forecast
            FROM forecasts
            WHERE period BETWEEN '{params['period_start']}' AND '{params['period_end']}'
            GROUP BY region, period
        """

        return {
            "actuals":   await fedsql.execute_to_reader(
                            params["actuals_source"], actuals_sql, actuals_ctx),
            "forecasts": await fedsql.execute_to_reader(
                            params["forecasts_source"], forecasts_sql, forecasts_ctx),
        }

    async def compute(self, inputs, params, ctx):
        # The runtime has already registered inputs["actuals"] and inputs["forecasts"]
        # as DuckDB tables in the connection it provides via ctx._duckdb.
        con = ctx._duckdb
        result = con.execute("""
            WITH joined AS (
              SELECT a.region, a.period, a.actual, f.forecast,
                     (a.actual - f.forecast) AS variance,
                     CASE WHEN f.forecast = 0 THEN NULL
                          ELSE (a.actual - f.forecast) / f.forecast END
                       AS variance_pct
              FROM actuals a
              LEFT JOIN forecasts f USING (region, period)
            )
            SELECT region, period, actual, forecast, variance, variance_pct
            FROM joined
            ORDER BY region, period
        """).arrow()

        # Aux: per-region summary
        summary = con.execute("""
            SELECT region,
                   SUM(actual) AS total_actual,
                   SUM(forecast) AS total_forecast,
                   SUM(actual - forecast) AS total_variance,
                   regr_slope(actual, EXTRACT(EPOCH FROM period)) AS actual_trend
            FROM joined GROUP BY region
        """).arrow()

        return ComputeResult(
            primary=result,
            auxiliaries={"by_region": summary},
            metadata={
                "row_count": result.num_rows,
                "regions": result.column("region").unique().to_pylist(),
            },
        )

    async def persist(self, result, params, ctx, store):
        return await store.write(
            job_id=ctx.job_id,
            primary=result.primary,
            auxiliaries=result.auxiliaries,
            metadata=result.metadata,
            params=params,
        )
```

Things to notice:

- **Compute is mostly SQL.** Variance decomposition is expressible as a CTE chain. `regr_slope` is a built-in DuckDB aggregate. No Python loops. No pandas. No NumPy.
- **Inputs are streams, not tables.** `RecordBatchReader` is lazy; DuckDB pulls from it on demand via `con.register("actuals", inputs["actuals"])`. No materialization in Python.
- **Budgets propagate.** `extract_row_budget` from `JobContext` becomes `max_rows` on the `QueryContext`. If `actuals` would return 5B rows, extract fails fast with a `BudgetExceededError`.
- **Runtime owns the DuckDB connection.** The recipe gets `ctx._duckdb` already configured with memory limits and threads, with input streams already registered. Recipe authors don't manage DuckDB lifecycle.

## A more analytical example: trend decomposition

```python
class TrendAnalysisRecipe:
    name = "trend_analysis"
    version = "1.0.0"

    def validate(self, params):
        if params.get("period_seasons", 12) < 4:
            raise ValueError("period_seasons must be >= 4 for STL")

    async def extract(self, params, ctx, fedsql):
        # Pull a time series per group
        sql = f"""
            SELECT {params['group_col']} AS grp,
                   {params['time_col']} AS ts,
                   {params['value_col']} AS value
            FROM {params['table']}
            WHERE {params['time_col']} BETWEEN '{params['start']}' AND '{params['end']}'
            ORDER BY {params['group_col']}, {params['time_col']}
        """
        return {"observations": await fedsql.execute_to_reader(
            params["source"], sql,
            QueryContext(tenant_id=ctx.tenant_id, query_id=ctx.job_id,
                         max_rows=ctx.extract_row_budget))}

    async def compute(self, inputs, params, ctx):
        con = ctx._duckdb
        # The analytical UDFs are pre-registered by the runtime.
        # stl_decompose(values, period) → struct{trend, seasonal, resid}
        result = con.execute(f"""
            WITH series AS (
              SELECT grp,
                     list(value ORDER BY ts) AS values,
                     list(ts    ORDER BY ts) AS timestamps
              FROM observations
              GROUP BY grp
            )
            SELECT grp, timestamps, values,
                   stl_decompose(values, {params.get('period_seasons', 12)}) AS decomp
            FROM series
        """).arrow()
        return ComputeResult(primary=result)

    async def persist(self, result, params, ctx, store):
        return await store.write(job_id=ctx.job_id, primary=result.primary, params=params)
```

The pattern that emerges across recipes:

1. SQL aggregates the time series per group into a list (DuckDB `list()` aggregate).
2. A Python UDF receives the list as `pa.Array`, runs the statistical method via NumPy/SciPy/statsmodels, returns a struct.
3. Result is a per-group structured table.

Authoring stays SQL-centric; statistical libraries appear inside UDFs only where SQL can't express the computation.

## Recipe registration

No global registry. Applications wire recipes into a runtime explicitly:

```python
from nexcraft_jobs.runtime.temporal import TemporalRuntime

runtime = TemporalRuntime(
    temporal_target="temporal:7233",
    namespace="default",
    task_queue="nexcraft-jobs",
    fedsql_client=client,
    result_store=store,
    recipes=[
        VarianceAnalysisRecipe(),
        TrendAnalysisRecipe(),
        # custom user recipes go here
    ],
)
await runtime.start()
```

The runtime registers each recipe as a Temporal workflow + activities. See [`02-temporal.md`](02-temporal.md).

## Recipe versioning

The `version` field is part of the workflow type. Two coexisting versions of `variance_analysis` (1.0.0 and 2.0.0) are two distinct workflow types in Temporal. In-flight 1.0.0 runs continue on 1.0.0; new submissions go to whichever version the submission API names. This is how Temporal's workflow versioning works and why we lean on it.
