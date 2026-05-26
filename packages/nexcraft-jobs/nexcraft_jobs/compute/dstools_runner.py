"""Run dstools tools against a nexcraft-jobs DuckDB connection.

Two entry points:

- ``run_sql_tool(con, name, params)``
    Render the SQL template, translate to DuckDB dialect, execute against `con`,
    return a pyarrow.Table.

- ``run_python_tool(name, params)``
    Look up the Python tool, invoke it with `params`, return the result
    (typically a pandas.DataFrame or a dstools TabularOutput).

The intent is that a recipe or Temporal activity in nexcraft-jobs does its own
data plumbing (registering Arrow tables / parquet views with `con`), then asks
this runner to execute a named tool from the dstools catalog. dstools owns the
analytical logic; nexcraft-jobs owns the data plane.
"""
from __future__ import annotations

from typing import Any

import duckdb
import pyarrow as pa

from dstools.execution.runner import execute_tool
from dstools.registry.metadata import ToolKind
from dstools.registry.registry import get_registry


def run_sql_tool(
    con: duckdb.DuckDBPyConnection,
    name: str,
    params: dict[str, Any],
) -> pa.Table:
    """Render `name`'s template against `params` and run it on `con`. Returns Arrow."""
    meta = get_registry().get(name)
    if meta.kind is not ToolKind.SQL_TEMPLATE:
        raise ValueError(f"tool {name!r} is not a SQL template (kind={meta.kind})")
    sql = execute_tool(name, params, dialect="duckdb")
    return con.execute(sql).to_arrow_table()


def run_python_tool(name: str, params: dict[str, Any]) -> Any:
    """Invoke a Python tool. Caller is responsible for materializing any DataFrame
    inputs from `con` and passing them via `params`."""
    meta = get_registry().get(name)
    if meta.kind is not ToolKind.PYTHON:
        raise ValueError(f"tool {name!r} is not a Python tool (kind={meta.kind})")
    return execute_tool(name, params)


def render_only(name: str, params: dict[str, Any], dialect: str = "duckdb") -> str:
    """Render a SQL template without executing it. Useful for previewing or for
    handing the SQL to a remote engine (Trino, Snowflake) instead of local DuckDB."""
    meta = get_registry().get(name)
    if meta.kind is not ToolKind.SQL_TEMPLATE:
        raise ValueError(f"tool {name!r} is not a SQL template (kind={meta.kind})")
    return execute_tool(name, params, dialect=dialect)
