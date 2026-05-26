"""CrossSourceFluxRecipe — the same compute body runs against any source.

extract:  fedsql.execute(source_id, "SELECT ... FROM <table> LIMIT n") → Arrow
compute:  register Arrow into DuckDB → call the SAME dstools tools regardless
          of where the data originated. The recipe body is source-agnostic;
          `source_id` is the only thing that changes when you target Postgres
          vs Snowflake (vs, later, a FlightSQL federation source).

Params (`Mapping[str, Any]`):
  source_id     str         Which FedSQL source to extract from.
  table         str         Source table reference (dialect-specific quoting).
  rate_col      str         Numeric column name in the extracted result.
  date_col      str         Timestamp / date column name.
  hospital_col  str         Categorical grouping dimension (optional).
  sample_rows   int         Row count to pull from the source (default 50_000).

The compute step runs:
  1. distribution_summary  (rate_col, grouped by hospital if provided)
  2. flux_variance         (rate_col by month, grouped by hospital)
  3. statistical_trend     (slope/intercept/R^2 of rate_col by month)
"""
from __future__ import annotations

from typing import Any, Mapping

import pyarrow as pa

from nexcraft.client import FedSQLClient
from nexcraft.core.context import QueryContext

from nexcraft_jobs.compute.dstools_runner import run_sql_tool
from nexcraft_jobs.context import JobContext
from nexcraft_jobs.recipe import ResultStore
from nexcraft_jobs.types import ComputeResult, ResultRef


_REQUIRED = ("source_id", "table", "rate_col", "date_col")


class CrossSourceFluxRecipe:
    name = "cross_source_flux"
    version = "v1"

    def validate(self, params: Mapping[str, Any]) -> None:
        missing = [k for k in _REQUIRED if not params.get(k)]
        if missing:
            raise ValueError(f"missing required params: {missing}")

    async def extract(
        self, params: Mapping[str, Any], ctx: JobContext, fedsql: FedSQLClient
    ) -> dict[str, pa.Table]:
        source_id   = params["source_id"]
        table       = params["table"]
        rate_col    = params["rate_col"]
        date_col    = params["date_col"]
        sample_rows = int(params.get("sample_rows", 50_000))

        # Pull a bounded sample. SELECT * keeps the recipe agnostic to the
        # source's exact column set — extra columns are harmless downstream.
        sql = f"SELECT * FROM {table} LIMIT {sample_rows}"
        qctx = QueryContext(tenant_id=ctx.tenant_id, query_id=f"{ctx.job_id}-extract")
        arrow_table = await fedsql.execute_to_table(source_id, sql, qctx)

        # Quick sanity: the columns we'll reference in compute must exist.
        cols_lower = {c.lower() for c in arrow_table.column_names}
        for c in (rate_col, date_col):
            if c.lower() not in cols_lower:
                raise ValueError(
                    f"column {c!r} not in extracted result. "
                    f"Available: {sorted(arrow_table.column_names)[:15]}…"
                )
        return {"facts": arrow_table}

    async def compute(
        self, inputs: dict[str, pa.Table], params: Mapping[str, Any], ctx: JobContext
    ) -> ComputeResult:
        rate_col     = params["rate_col"]
        date_col     = params["date_col"]
        hospital_col = params.get("hospital_col")

        con = ctx._duckdb  # registered by the runtime; `facts` view already present
        group_cols = [hospital_col] if hospital_col else None

        # 1. distribution_summary — same tool name + same params for any source.
        dist = run_sql_tool(con, "distribution_summary", {
            "table":      "facts",
            "value_col":  rate_col,
            "group_cols": group_cols,
        }).to_pandas()

        # 2. flux_variance MoM. Dimensions defaults to [] (whole-table flux)
        #    when no hospital_col is given.
        flux = run_sql_tool(con, "flux_variance", {
            "table":         "facts",
            "amount_col":    rate_col,
            "date_col":      date_col,
            "dimensions":    group_cols or [],
            "filter_clause": f"{date_col} IS NOT NULL",
            "material_pct":  0.20,
            "grain":         "month",
        }).to_pandas()

        # 3. statistical_trend per group.
        trend = run_sql_tool(con, "statistical_trend", {
            "table":      "facts",
            "value_col":  rate_col,
            "time_col":   date_col,
            "group_cols": group_cols,
            "grain":      "month",
        }).to_pandas()

        # Persist the three results as a single multi-table ComputeResult.
        return ComputeResult(
            primary=pa.Table.from_pandas(dist),
            auxiliaries={
                "flux_variance":     pa.Table.from_pandas(flux),
                "statistical_trend": pa.Table.from_pandas(trend),
            },
            metadata={
                "recipe":     self.name,
                "source_id":  params["source_id"],
                "table":      params["table"],
                "n_extracted": inputs["facts"].num_rows,
            },
        )

    async def persist(self, result, params, ctx, store: ResultStore) -> ResultRef:
        return await store.finalize(ctx, result, params)
