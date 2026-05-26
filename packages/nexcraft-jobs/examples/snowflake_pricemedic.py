"""End-to-end PriceMedic example: run a battery of dstools tools against the
real Snowflake-Marketplace listing
https://app.snowflake.com/marketplace/listing/GZT1Z36FBPY/pricemedic-pricemedic-core-hospital-health-system-rates

Run from the nexcraft-jobs package root after populating `.env` (see .env.example):

    set -a; source .env; set +a
    python examples/snowflake_pricemedic.py

The script:
  1. Connects to Snowflake using env-var credentials.
  2. Materializes a row-count sample of the configured view into a TEMP table
     (the full view is ~90M rows, way too big to scan repeatedly). All six
     downstream tools run against this sample so the example completes in
     reasonable time and uses a consistent row set.
  3. Runs ~6 representative tools end-to-end, printing each result.
"""
from __future__ import annotations

import os
import sys
from textwrap import indent

import pandas as pd

from nexcraft_jobs.compute.snowflake_runner import (
    credentials_available,
    fetch_query,
    get_snowflake_connection,
    materialize_sample,
    render_only_snowflake,
    run_python_tool_snowflake,
    run_sql_tool_snowflake,
)


def _env_or_die(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        print(f"Missing env var: {name}. Populate it via .env (see .env.example).",
              file=sys.stderr)
        sys.exit(2)
    return v


def main() -> int:
    if not credentials_available():
        print("Snowflake credentials not set; see .env.example.", file=sys.stderr)
        return 2

    source_table = _env_or_die("PRICEMEDIC_TABLE")
    rate_col     = _env_or_die("PRICEMEDIC_RATE_COL")
    date_col     = _env_or_die("PRICEMEDIC_DATE_COL")
    hospital     = _env_or_die("PRICEMEDIC_HOSPITAL_COL")
    procedure    = _env_or_die("PRICEMEDIC_PROCEDURE_COL")
    payer        = _env_or_die("PRICEMEDIC_PAYER_COL")
    sample_rows  = int(os.environ.get("PRICEMEDIC_SAMPLE_ROWS", "500000"))

    pd.set_option("display.max_columns", 20)
    pd.set_option("display.width", 160)

    con = get_snowflake_connection()
    print(f"Connected to Snowflake: {con.account} / {con.database}.{con.schema}")
    try:
        print(f"Materializing {sample_rows:,}-row sample from {source_table} …")
        table = materialize_sample(
            con,
            source_table=source_table,
            n_rows=sample_rows,
            # Marketplace data often stores the date as VARCHAR — cast at
            # the boundary so DATE_TRUNC works in every template. Skip the
            # rate cast: TRY_TO_NUMBER would fail on an already-numeric
            # column (Snowflake refuses FLOAT → NUMBER via TRY_CAST).
            cast_columns={date_col: "TRY_TO_TIMESTAMP"},
        )
        n = fetch_query(con, f"SELECT COUNT(*) AS N FROM {table}").iloc[0, 0]
        print(f"  → temp table `{table}` ready with {int(n):,} rows.\n")

        # 1. Distribution summary of rates by hospital — SQL lane.
        print("=== 1. distribution_summary of rate by hospital (SQL) ===")
        df = run_sql_tool_snowflake(con, "distribution_summary", {
            "table":      table,
            "value_col":  rate_col,
            "group_cols": [hospital],
        })
        print(indent(df.head(10).to_string(index=False), "  "))
        print()

        # 2. Outliers by procedure via IQR — SQL lane.
        print("=== 2. outliers_iqr per procedure, top 20 outlier rows (SQL) ===")
        sql = render_only_snowflake("outliers_iqr", {
            "table":          table,
            "value_col":      rate_col,
            "group_cols":     [procedure],
            "iqr_multiplier": 3.0,
        })
        df = fetch_query(con, f"SELECT * FROM ({sql.rstrip(';')}) WHERE is_outlier = 1 LIMIT 20")
        print(indent(df.to_string(index=False), "  "))
        print()

        # 3. Flux variance MoM by hospital + payer — SQL lane.
        print("=== 3. flux_variance MoM by (hospital, payer) (SQL) ===")
        df = run_sql_tool_snowflake(con, "flux_variance", {
            "table":         table,
            "amount_col":    rate_col,
            "date_col":      date_col,
            "dimensions":    [hospital, payer],
            "filter_clause": "TRUE",
            "material_pct":  0.20,
            "grain":         "month",
        })
        print(indent(df.head(15).to_string(index=False), "  "))
        print()

        # 4. Statistical trend per procedure — SQL lane.
        print("=== 4. statistical_trend (slope/intercept/R^2) per procedure (SQL) ===")
        df = run_sql_tool_snowflake(con, "statistical_trend", {
            "table":      table,
            "value_col":  rate_col,
            "time_col":   date_col,
            "group_cols": [procedure],
            "grain":      "month",
        })
        print(indent(df.head(10).to_string(index=False), "  "))
        print()

        # 5. Python-lane: historical_var on a 5000-row slice of the sample.
        print("=== 5. historical_var_pd on a 5000-row slice (Python) ===")
        out = run_python_tool_snowflake(
            con,
            query=f"SELECT {rate_col} AS RATE FROM {table} SAMPLE (5000 ROWS)",
            name="historical_var_pd",
            params={"returns_col": "RATE", "alpha": 0.05},
        )
        df = pd.DataFrame(out.data) if hasattr(out, "data") else out
        print(indent(df.to_string(index=False), "  "))
        print()

        # 6. Drift check: PSI between two halves of the sample by date (SQL).
        # NTILE(2) over the date column splits the sample into equal halves
        # without needing PERCENTILE_CONT on a TIMESTAMP (which Snowflake
        # rejects — PERCENTILE_CONT is numeric-only on Snowflake).
        print("=== 6. PSI: first vs second half of the sample by date (SQL) ===")
        cur = con.cursor()
        try:
            cur.execute(
                f"CREATE OR REPLACE TEMP VIEW _pm_halves AS "
                f"SELECT *, NTILE(2) OVER (ORDER BY {date_col}) AS half FROM {table}"
            )
            cur.execute("CREATE OR REPLACE TEMP VIEW pricemedic_baseline AS "
                        "SELECT * FROM _pm_halves WHERE half = 1")
            cur.execute("CREATE OR REPLACE TEMP VIEW pricemedic_current AS "
                        "SELECT * FROM _pm_halves WHERE half = 2")
        finally:
            cur.close()
        df = run_sql_tool_snowflake(con, "psi", {
            "baseline_table": "pricemedic_baseline",
            "current_table":  "pricemedic_current",
            "value_col":      rate_col,
            "n_bins":         10,
        })
        print(indent(df.to_string(index=False), "  "))
        print()

    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
