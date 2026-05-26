"""Same cross-source flow as `cross_source_recipe.py`, but routed through the
nexcraft-driver Flight gRPC endpoint instead of LocalRuntime.

This proves the driver's portability claim end-to-end: a single client API
(`DriverClient`) drives Postgres / Snowflake / Delta / Iceberg without code
changes, and the dstools compute body runs unchanged on the resulting Arrow.

The script spins up the driver in-process on a free port so you can run it
with a single command — no separate terminal needed. For a more realistic
deployment, replace the in-process server boot with a connection to a
remote driver (`DriverClient("grpc://nexcraft-driver:50051")`).

Run:
    set -a; source .env; set +a
    python examples/cross_source_via_driver.py
"""
from __future__ import annotations

import asyncio
import os
# Tell Acero (the Arrow compute engine DuckDB uses under con.register) to
# reallocate unaligned buffers instead of warning. The Snowflake connector
# returns Arrow batches whose buffers aren't 64-byte aligned; on x86/ARM
# this is harmless but every Acero source node logs a noisy warning. Setting
# this before any pyarrow.compute / DuckDB scan happens silences the noise
# at the cost of one tiny realloc per misaligned buffer.
os.environ.setdefault("ACERO_ALIGNMENT_HANDLING", "reallocate")

import socket
import sys
import threading
import time
from pathlib import Path
from textwrap import indent

import duckdb
import pandas as pd

from nexcraft_driver.async_store import InProcessAsyncQueryStore
from nexcraft_driver.auth import AuthMiddlewareFactory
from nexcraft_driver.client import DriverClient
from nexcraft_driver.integration import (
    DELTA_SOURCE_ID,
    ICEBERG_SOURCE_ID,
    LAKEHOUSE_VIEW_NAME,
    POSTGRES_SOURCE_ID,
    SNOWFLAKE_SOURCE_ID,
    build_cross_source_fedsql,
)
from nexcraft_driver.server import DriverFlightServer
from nexcraft_driver.types import QueryState

from nexcraft_jobs.compute.dstools_runner import run_sql_tool


def _free_port() -> int:
    s = socket.socket(); s.bind(("", 0))
    try:
        return s.getsockname()[1]
    finally:
        s.close()


def _have(*envs: str) -> bool:
    return all(os.environ.get(e) for e in envs)


async def _start_driver(spool: Path) -> tuple[DriverFlightServer, str]:
    fedsql, _ = await build_cross_source_fedsql()
    store = InProcessAsyncQueryStore(fedsql, spool_dir=spool)
    port = _free_port()
    location = f"grpc://127.0.0.1:{port}"
    server = DriverFlightServer(
        fedsql=fedsql,
        store=store,
        auth=AuthMiddlewareFactory(insecure=True),
        location=location,
    )
    threading.Thread(target=server.serve, daemon=True).start()
    time.sleep(0.3)  # let gRPC bind
    return server, location


def _compute_on_arrow(arrow_table, *, rate_col: str, date_col: str,
                      group_col: str | None) -> dict[str, pd.DataFrame]:
    """Run the same three dstools tools the recipe runs, against the Arrow
    table the driver returned. Identical compute body — only the data
    transport changed (FlightSQL instead of in-process FedSQLClient)."""
    # combine_chunks() reallocates the table with aligned buffers — Snowflake's
    # Arrow output can be misaligned, which makes DuckDB/Acero spew warnings
    # like "An input buffer was poorly aligned". One-time copy, silences them.
    arrow_table = arrow_table.combine_chunks()

    con = duckdb.connect(":memory:")
    con.register("_facts_raw", arrow_table)
    # Some marketplace datasets store dates as VARCHAR; TRY_CAST handles both
    # already-typed timestamps (no-op) and string-encoded ones (parsed, NULL
    # on failure). Build a `facts` table with the date column coerced to
    # TIMESTAMP so downstream DATE_TRUNC works for every source.
    con.execute(f"""
        CREATE OR REPLACE TABLE facts AS
        SELECT * EXCLUDE ({date_col}),
               TRY_CAST({date_col} AS TIMESTAMP) AS {date_col}
        FROM _facts_raw
    """)
    group_cols = [group_col] if group_col else None

    dist  = run_sql_tool(con, "distribution_summary", {
        "table": "facts", "value_col": rate_col, "group_cols": group_cols,
    }).to_pandas()
    flux  = run_sql_tool(con, "flux_variance", {
        "table": "facts", "amount_col": rate_col, "date_col": date_col,
        "dimensions": group_cols or [],
        "filter_clause": f"{date_col} IS NOT NULL",
        "material_pct": 0.20, "grain": "month",
    }).to_pandas()
    trend = run_sql_tool(con, "statistical_trend", {
        "table": "facts", "value_col": rate_col, "time_col": date_col,
        "group_cols": group_cols, "grain": "month",
    }).to_pandas()
    return {"distribution_summary": dist, "flux_variance": flux, "statistical_trend": trend}


def _print_blocks(label: str, blocks: dict[str, pd.DataFrame]) -> None:
    print(f"=== {label} ===")
    for name, df in blocks.items():
        print(f"  -- {name} ({len(df)} rows) --")
        print(indent(df.head(10).to_string(index=False), "    "))
        print()


def _run_one_source(
    client: DriverClient, source_id: str, label: str, *,
    table: str, rate_col: str, date_col: str, group_col: str | None,
    sample_rows: int, use_async: bool,
) -> None:
    print(f"\n############ {label} (source_id={source_id}, mode={'async' if use_async else 'sync'}) ############")
    sql = f"SELECT * FROM {table} LIMIT {sample_rows}"
    try:
        if use_async:
            handle = client.submit(source_id, sql)
            for _ in range(120):
                st = client.status(handle)
                if st.state in (QueryState.SUCCEEDED, QueryState.FAILED, QueryState.CANCELLED):
                    break
                time.sleep(0.5)
            if st.state is not QueryState.SUCCEEDED:
                print(f"  driver returned state={st.state.value} error={st.error_message}")
                return
            arrow_table = client.fetch(handle)
        else:
            arrow_table = client.execute_sync(source_id, sql)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED ({type(exc).__name__}): {exc}")
        return
    print(f"  Extracted {arrow_table.num_rows} rows × {arrow_table.num_columns} cols via driver.")
    blocks = _compute_on_arrow(arrow_table, rate_col=rate_col, date_col=date_col, group_col=group_col)
    _print_blocks(label, blocks)


async def amain() -> int:
    pd.set_option("display.max_columns", 20)
    pd.set_option("display.width", 160)

    spool = Path("./_async_results_driver_demo")
    spool.mkdir(exist_ok=True)
    server, location = await _start_driver(spool)
    print(f"Driver listening on {location}\n")
    client = DriverClient(location)
    try:
        if _have("POSTGRES_HOST", "POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD",
                 "CORNERSTONE_TABLE", "CORNERSTONE_SCORE_COL", "CORNERSTONE_TIME_COL"):
            _run_one_source(
                client, POSTGRES_SOURCE_ID, "Postgres (Cornerstone)",
                table=os.environ["CORNERSTONE_TABLE"],
                rate_col=os.environ["CORNERSTONE_SCORE_COL"],
                date_col=os.environ["CORNERSTONE_TIME_COL"],
                group_col=os.environ.get("CORNERSTONE_STATUS_COL"),
                sample_rows=int(os.environ.get("CORNERSTONE_SAMPLE_ROWS", "10000")),
                use_async=False,
            )

        if _have("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_DATABASE",
                 "SNOWFLAKE_SCHEMA", "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_PASSWORD",
                 "PRICEMEDIC_TABLE", "PRICEMEDIC_RATE_COL", "PRICEMEDIC_DATE_COL"):
            _run_one_source(
                client, SNOWFLAKE_SOURCE_ID, "Snowflake (PriceMedic)",
                table=os.environ["PRICEMEDIC_TABLE"],
                rate_col=os.environ["PRICEMEDIC_RATE_COL"],
                date_col=os.environ["PRICEMEDIC_DATE_COL"],
                group_col=os.environ.get("PRICEMEDIC_HOSPITAL_COL"),
                sample_rows=int(os.environ.get("PRICEMEDIC_SAMPLE_ROWS", "10000")),
                use_async=True,  # exercise the async path for one source
            )

        if _have("DELTA_TABLE_S3_PATH", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            _run_one_source(
                client, DELTA_SOURCE_ID, "Delta Lake on S3",
                table=LAKEHOUSE_VIEW_NAME,
                rate_col=os.environ.get("DELTA_RATE_COL", "metric_val"),
                date_col=os.environ.get("DELTA_DATE_COL", "metric_ts"),
                group_col=os.environ.get("DELTA_GROUP_COL"),
                sample_rows=int(os.environ.get("DELTA_SAMPLE_ROWS", "10000")),
                use_async=False,
            )

        if _have("ICEBERG_TABLE_S3_PATH", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            _run_one_source(
                client, ICEBERG_SOURCE_ID, "Iceberg on S3",
                table=LAKEHOUSE_VIEW_NAME,
                rate_col=os.environ.get("ICEBERG_RATE_COL", "metric_val"),
                date_col=os.environ.get("ICEBERG_DATE_COL", "metric_ts"),
                group_col=os.environ.get("ICEBERG_GROUP_COL"),
                sample_rows=int(os.environ.get("ICEBERG_SAMPLE_ROWS", "10000")),
                use_async=False,
            )

    finally:
        client.close()
        server.shutdown()

    print("Done — same dstools compute body ran against every configured source through the driver.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
