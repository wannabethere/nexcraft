from __future__ import annotations

import duckdb
import pyarrow as pa

from nexcraft_jobs.context import JobContext


def setup_duckdb(ctx: JobContext) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(database=":memory:")
    con.execute(f"SET memory_limit = '{ctx.memory_budget}'")
    con.execute(f"SET threads = {ctx.cpu_budget}")
    if ctx.scratch_dir:
        con.execute(f"SET temp_directory = '{ctx.scratch_dir}'")
    con.execute("SET preserve_insertion_order = false")
    return con


def register_extract_streams(
    con: duckdb.DuckDBPyConnection,
    streams: dict[str, pa.RecordBatchReader | pa.Table],
) -> None:
    """LocalRuntime path: register Arrow inputs. Temporal path uses read_parquet views instead."""
    for name, obj in streams.items():
        if not name.replace("_", "").isalnum():
            raise ValueError(f"Unsafe DuckDB registration name: {name!r}")
        con.register(name, obj)


def _sql_single_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def register_extract_views_from_parquet(
    con: duckdb.DuckDBPyConnection,
    datasets: dict[str, str],
) -> None:
    """Temporal extract staging: map logical name -> parquet URI (see jobs/02-temporal.md)."""
    for name, uri in datasets.items():
        if not name.replace("_", "").isalnum():
            raise ValueError(f"Unsafe view name: {name!r}")
        lit = _sql_single_quoted(uri)
        con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet({lit})")
