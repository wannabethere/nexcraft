"""End-to-end smoke test of dstools running on a nexcraft-jobs DuckDB connection.

Run from the nexcraft-jobs package root:

    python examples/dstools_smoke.py

Prereq: dstools installed in the same env. Easiest while dstools is unreleased:

    pip install -e ../../../genieml/dstools

The example builds a tiny event table, then calls:
  1. The SQL template `cohort_retention` (rendered + executed on DuckDB).
  2. The pure-Python tool `cohort_retention_pd` (pandas in-process).
Both should return the same retention numbers — that's the whole point of
keeping two lanes in one registry.
"""
from __future__ import annotations

from datetime import datetime

import duckdb
import pyarrow as pa

from nexcraft_jobs.compute.dstools_runner import (
    render_only,
    run_python_tool,
    run_sql_tool,
)


def _make_events() -> pa.Table:
    rows = [
        ("u1", datetime(2026, 1, 5)),
        ("u1", datetime(2026, 2, 10)),
        ("u1", datetime(2026, 3, 1)),
        ("u2", datetime(2026, 1, 20)),
        ("u2", datetime(2026, 2, 15)),
        ("u3", datetime(2026, 2, 3)),
        ("u3", datetime(2026, 3, 4)),
        ("u4", datetime(2026, 3, 11)),
    ]
    return pa.table(
        {
            "user_id": pa.array([r[0] for r in rows]),
            "ts":      pa.array([r[1] for r in rows]),
        }
    )


def main() -> None:
    events = _make_events()
    con = duckdb.connect(database=":memory:")
    con.register("events", events)

    params = {
        "events_table":   "events",
        "user_col":       "user_id",
        "event_time_col": "ts",
        "event_filter":   "TRUE",
        "cohort_grain":   "month",
        "period_grain":   "month",
    }

    print("=== Rendered SQL (DuckDB dialect) ===")
    print(render_only("cohort_retention", params))

    print("\n=== SQL lane: run_sql_tool('cohort_retention') ===")
    sql_result = run_sql_tool(con, "cohort_retention", params)
    print(sql_result.to_pandas())

    print("\n=== Python lane: run_python_tool('cohort_retention_pd') ===")
    df_events = con.execute("SELECT * FROM events").df()
    py_result = run_python_tool(
        "cohort_retention_pd",
        {
            "df":             df_events,
            "user_col":       "user_id",
            "event_time_col": "ts",
            "cohort_grain":   "month",
            "period_grain":   "month",
        },
    )
    print(py_result)

    print("\n=== Same numbers? ===")
    print("If the cohort/period/retention columns match, the two lanes agree.")


if __name__ == "__main__":
    main()
