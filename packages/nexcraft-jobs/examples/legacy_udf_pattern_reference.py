#!/usr/bin/env python3
"""REFERENCE ONLY — legacy DuckDB Python-UDF pattern.

This example is kept in the live `examples/` folder so the shape of the old
warehouse → DuckDB → Python-UDF flow stays discoverable. It does **not** run
on the current codebase because the `nexcraft_jobs.compute.udfs` package has
been archived (see `_archive/README.md`).

If you genuinely need this pattern for a future use case:
  1. Read `_archive/README.md` for revival steps.
  2. For 99% of analytics work, prefer the `dstools` lane:
       - SQL templates: `dstools/sql/templates/` (rendered via SQLGlot to any
         engine — DuckDB, Snowflake, Trino).
       - Python tools: `dstools/py/*.py` (called via
         `nexcraft_jobs.compute.dstools_runner.run_python_tool`).
     The equivalent of the SMA call below is the SQL template
     `windows.moving_average` or the Python tool `moving_average_pd`. Both
     return the same numbers; see `tests/test_cross_lane_parity.py` for proof.

------------------------------------------------------------------------
Pattern recap (what the body below illustrates):

  warehouse rows
    └─► loaded into pa.Table
        └─► con.register('fact_metric', table)
            └─► GROUP BY entity → ordered lists → JSON `p_data` cell
                └─► UDF call: SELECT calculate_sma(p_data, window, group_by)
                    └─► STRUCT[] result, unnested by caller
------------------------------------------------------------------------
"""

from __future__ import annotations

import json

import duckdb


def _register_udfs(con: duckdb.DuckDBPyConnection) -> None:
    # The import below will fail on the current codebase. Kept verbatim so the
    # call shape is documented; revive only by following _archive/README.md.
    from nexcraft_jobs.compute.udfs import register_analytical_udfs  # type: ignore[import-not-found]
    register_analytical_udfs(con)


def _fake_fact_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE OR REPLACE TABLE fact_metric AS
        SELECT * FROM (VALUES
          (1::BIGINT, TIMESTAMP '2024-01-01', 10.0::DOUBLE),
          (1::BIGINT, TIMESTAMP '2024-01-02', 20.0::DOUBLE),
          (1::BIGINT, TIMESTAMP '2024-01-03', 15.0::DOUBLE),
          (2::BIGINT, TIMESTAMP '2024-01-01', 100.0::DOUBLE),
          (2::BIGINT, TIMESTAMP '2024-01-02', 110.0::DOUBLE),
          (2::BIGINT, TIMESTAMP '2024-01-03', 105.0::DOUBLE)
        ) AS t(entity_id, metric_ts, metric_val);
        """
    )


def main() -> int:
    con = duckdb.connect(":memory:")
    _register_udfs(con)             # ← will raise ImportError on current tree
    _fake_fact_table(con)

    # Pattern: GROUP BY entity → list_timeseries_to_json → calculate_sma.
    rows = con.execute(
        """
        SELECT entity_id,
               list_timeseries_to_json(
                 list(metric_ts ORDER BY metric_ts),
                 list(metric_val ORDER BY metric_ts)
               ) AS p_data_json
        FROM fact_metric
        GROUP BY entity_id
        ORDER BY entity_id;
        """
    ).fetchall()

    print("--- invoke_sql_function('calculate_sma', payload) ---")
    for entity_id, p_data_json in rows:
        payload = json.dumps(
            {"p_data": json.loads(p_data_json), "p_window_size": 2, "p_group_by": ""},
            default=str,
        )
        out = con.execute(
            "SELECT invoke_sql_function(?, ?)",
            ["calculate_sma", payload],
        ).fetchone()[0]
        print(f"entity {entity_id}: {str(out)[:200]}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
