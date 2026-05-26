"""Activities: ODBC fetch (Cornerstone / SQL Server) + in-process DuckDB combine."""

from __future__ import annotations

import asyncio
from typing import Any

from temporalio import activity

from dstools.contracts.outputs import TabularOutput

from nexcraft_jobs.runtime.multihop_models import DuckDbCombineInput, SqlServerFetchInput


def _build_odbc_conn_str(inp: SqlServerFetchInput) -> str:
    trust = "yes" if inp.trust_server_certificate else "no"
    return (
        f"DRIVER={{{inp.odbc_driver}}};"
        f"SERVER={inp.server};"
        f"DATABASE={inp.database};"
        f"UID={inp.uid};"
        f"PWD={inp.pwd};"
        f"Encrypt={inp.encrypt};"
        f"TrustServerCertificate={trust};"
    )


def _fetch_sql_server_sync(inp: SqlServerFetchInput) -> list[dict[str, Any]]:
    import pyodbc  # type: ignore[import-untyped]

    conn_str = _build_odbc_conn_str(inp)
    with pyodbc.connect(conn_str, timeout=60) as conn:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(inp.sql)
        columns = [c[0] for c in cur.description] if cur.description else []
        rows: list[dict[str, Any]] = []
        batch = cur.fetchmany(5000)
        while batch:
            for tup in batch:
                rows.append(dict(zip(columns, tup)))
            batch = cur.fetchmany(5000)
        return rows


@activity.defn(name="nexcraft.multihop.fetch_sql_server_rows")
async def fetch_sql_server_rows(inp: SqlServerFetchInput | dict[str, Any]) -> list[dict[str, Any]]:
    """Blocking ODBC read offloaded to the default executor."""
    payload = inp if isinstance(inp, SqlServerFetchInput) else SqlServerFetchInput(**inp)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _fetch_sql_server_sync(payload))


def _duckdb_combine_sync(inp: DuckDbCombineInput) -> TabularOutput:
    import time

    import duckdb  # type: ignore[import-untyped]
    import pandas as pd

    started = time.perf_counter()
    hop1 = pd.DataFrame(inp.hop1_rows)
    hop2 = pd.DataFrame(inp.hop2_rows)
    con = duckdb.connect(database=":memory:")
    try:
        con.register(inp.hop1_table, hop1)
        con.register(inp.hop2_table, hop2)
        out_df = con.execute(inp.combine_sql).fetchdf()
    finally:
        con.close()

    elapsed_ms = (time.perf_counter() - started) * 1000
    return TabularOutput(
        tool="nexcraft.multihop.duckdb_combine_hops",
        rows_returned=len(out_df),
        elapsed_ms=elapsed_ms,
        schema={c: str(t) for c, t in out_df.dtypes.items()},
        data=out_df.to_dict(orient="records"),
    )


@activity.defn(name="nexcraft.multihop.duckdb_combine_hops")
async def duckdb_combine_hops(inp: DuckDbCombineInput | dict[str, Any]) -> TabularOutput | dict[str, Any]:
    payload = inp if isinstance(inp, DuckDbCombineInput) else DuckDbCombineInput(**inp)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _duckdb_combine_sync(payload))
