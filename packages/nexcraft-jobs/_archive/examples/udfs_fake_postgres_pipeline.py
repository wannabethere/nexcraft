#!/usr/bin/env python3
"""
Simulate: warehouse rows (as from Postgres) → DuckDB → ``list_*_to_json`` → UDFs.

No live database required. Run from ``nexcraft`` repo root::

    PYTHONPATH=packages/nexcraft:packages/nexcraft-jobs \\
      python packages/nexcraft-jobs/examples/udfs_fake_postgres_pipeline.py

**Wiring a real Postgres:** run any ``SELECT entity_id, metric_ts, metric_val FROM ...`` via
asyncpg/psycopg, build a ``pyarrow.Table``, then ``con.register('fact_metric', table)`` and reuse
the same DuckDB SQL below (same pattern as ``LocalRuntime`` + ``register_analytical_udfs``).
"""

from __future__ import annotations

import json
import sys

import duckdb


def _register_udfs(con: duckdb.DuckDBPyConnection) -> None:
    from nexcraft_jobs.compute.udfs import register_analytical_udfs

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
    _register_udfs(con)
    _fake_fact_table(con)

    # Same pattern as FedSQL recipes: GROUP BY entity → ordered lists → JSON ``p_data`` cell.
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

    print("--- list_timeseries_to_json per entity ---")
    for entity_id, p_data_json in rows:
        tail = "..." if len(str(p_data_json)) > 120 else ""
        print(entity_id, str(p_data_json)[:120], tail)

    print("\n--- invoke_sql_function('calculate_sma', payload) ---")
    for entity_id, p_data_json in rows:
        payload = json.dumps(
            {"p_data": json.loads(p_data_json), "p_window_size": 2, "p_group_by": ""},
            default=str,
        )
        out = con.execute(
            "SELECT invoke_sql_function(?, ?)",
            ["calculate_sma", payload],
        ).fetchone()[0]
        tail = "..." if len(str(out)) > 200 else ""
        print(f"entity {entity_id}: {str(out)[:200]}{tail}")

    print("\n--- native calculate_sma (VARCHAR p_data JSON + window + group) ---")
    for entity_id, p_data_json in rows:
        out = con.execute(
            "SELECT calculate_sma(?, 2, '')",
            [p_data_json],
        ).fetchone()[0]
        print(f"entity {entity_id}: {str(out)[:120]}...")

    print("\n--- list_metric_series_to_json (``metric`` key for trend-style rows) ---")
    row = con.execute(
        """
        SELECT list_metric_series_to_json(
                 list(metric_ts ORDER BY metric_ts),
                 list(metric_val ORDER BY metric_ts)
               )
        FROM fact_metric WHERE entity_id = 1;
        """
    ).fetchone()[0]
    print(str(row)[:200])

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
