"""End-to-end Cornerstone Learning example: run a battery of dstools tools
against a real Postgres instance.

The Cornerstone metadata under
`genieml/data/sql_meta/cornerstone_learning/` describes three tables in
schema `csod_learn_datamodel`:

  - Transcript_csod   user × activity learning records (score, timeSpent, completionDate)
  - Activity_csod     learning-object dimension
  - User_csod         user dimension (division, location, startDate)

This script targets Transcript_csod for the SQL/Python flow. Run from the
nexcraft-jobs package root after populating `.env`:

    set -a; source .env; set +a
    python examples/postgres_cornerstone.py
"""
from __future__ import annotations

import os
import sys
from textwrap import indent

import pandas as pd

from nexcraft_jobs.compute.postgres_runner import (
    credentials_available,
    fetch_query,
    get_postgres_connection,
    materialize_sample,
    render_only_postgres,
    run_python_tool_postgres,
    run_sql_tool_postgres,
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
        print("Postgres credentials not set; see .env.example.", file=sys.stderr)
        return 2

    source_table = _env_or_die("CORNERSTONE_TABLE")
    score_col    = _env_or_die("CORNERSTONE_SCORE_COL")
    time_col     = _env_or_die("CORNERSTONE_TIME_COL")
    status_col   = _env_or_die("CORNERSTONE_STATUS_COL")
    activity_col = _env_or_die("CORNERSTONE_ACTIVITY_COL")
    user_col     = _env_or_die("CORNERSTONE_USER_COL")
    sample_rows  = int(os.environ.get("CORNERSTONE_SAMPLE_ROWS", "100000"))

    pd.set_option("display.max_columns", 20)
    pd.set_option("display.width", 160)

    con = get_postgres_connection()
    print(f"Connected to Postgres: {os.environ['POSTGRES_HOST']}/{os.environ['POSTGRES_DB']}")
    try:
        print(f"Materializing {sample_rows:,}-row sample from {source_table} …")
        # No casts needed for cornerstone — most columns are already typed.
        # If you find a stringy timestamp, add it: cast_columns={time_col: "TIMESTAMP"}
        table = materialize_sample(
            con,
            source_table=source_table,
            sample_name="cornerstone_sample",
            n_rows=sample_rows,
        )
        n = fetch_query(con, f'SELECT COUNT(*) AS n FROM "{table}"').iloc[0, 0]
        print(f"  → temp table `{table}` ready with {int(n):,} rows.\n")

        # 1. Distribution summary of score by training status.
        print(f"=== 1. distribution_summary of {score_col} by {status_col} (SQL) ===")
        df = run_sql_tool_postgres(con, "distribution_summary", {
            "table":      table,
            "value_col":  score_col,
            "group_cols": [status_col],
        })
        print(indent(df.head(10).to_string(index=False), "  "))
        print()

        # 2. Outliers per learning object via IQR.
        print(f"=== 2. outliers_iqr per {activity_col} on {score_col} (SQL, top 20) ===")
        sql = render_only_postgres("outliers_iqr", {
            "table":          table,
            "value_col":      score_col,
            "group_cols":     [activity_col],
            "iqr_multiplier": 3.0,
        })
        df = fetch_query(con, f"SELECT * FROM ({sql.rstrip(';')}) sub WHERE is_outlier = 1 LIMIT 20")
        print(indent(df.head(20).to_string(index=False), "  "))
        print()

        # 3. Flux variance MoM by training status.
        print(f"=== 3. flux_variance MoM by {status_col} (SQL) ===")
        df = run_sql_tool_postgres(con, "flux_variance", {
            "table":         table,
            "amount_col":    score_col,
            "date_col":      time_col,
            "dimensions":    [status_col],
            "filter_clause": f"{time_col} IS NOT NULL",
            "material_pct":  0.20,
            "grain":         "month",
        })
        print(indent(df.head(15).to_string(index=False), "  "))
        print()

        # 4. Statistical trend per learning object.
        print(f"=== 4. statistical_trend per {activity_col} by month (SQL) ===")
        df = run_sql_tool_postgres(con, "statistical_trend", {
            "table":      table,
            "value_col":  score_col,
            "time_col":   time_col,
            "group_cols": [activity_col],
            "grain":      "month",
        })
        print(indent(df.head(10).to_string(index=False), "  "))
        print()

        # 5. Python-lane: historical_var on a 5000-row slice.
        print(f"=== 5. historical_var_pd on a 5000-row slice of {score_col} (Python) ===")
        out = run_python_tool_postgres(
            con,
            query=f'SELECT {score_col} AS score FROM "{table}" LIMIT 5000',
            name="historical_var_pd",
            params={"returns_col": "score", "alpha": 0.05},
        )
        df = pd.DataFrame(out.data) if hasattr(out, "data") else out
        print(indent(df.to_string(index=False), "  "))
        print()

        # 6. Drift check: PSI between two halves of the sample by date.
        print("=== 6. PSI: first vs second half of the sample by date (SQL) ===")
        cur = con.cursor()
        try:
            cur.execute(
                f'CREATE OR REPLACE VIEW _pm_halves AS '
                f'SELECT *, NTILE(2) OVER (ORDER BY {time_col}) AS half FROM "{table}"'
            )
            cur.execute('CREATE OR REPLACE VIEW cornerstone_baseline AS '
                        'SELECT * FROM _pm_halves WHERE half = 1')
            cur.execute('CREATE OR REPLACE VIEW cornerstone_current AS '
                        'SELECT * FROM _pm_halves WHERE half = 2')
            con.commit()
        finally:
            cur.close()
        df = run_sql_tool_postgres(con, "psi", {
            "baseline_table": "cornerstone_baseline",
            "current_table":  "cornerstone_current",
            "value_col":      score_col,
            "n_bins":         10,
        })
        print(indent(df.to_string(index=False), "  "))
        print()

    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
