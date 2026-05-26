"""Run dstools tools against Snowflake.

Mirrors `dstools_runner.py` for the DuckDB path. Two entry points:

- `run_sql_tool_snowflake(con, name, params)`
    Render the SQL template in Snowflake dialect (via SQLGlot), execute against
    `con`, return a pandas DataFrame.

- `run_python_tool_snowflake(con, query, name, params)`
    Pull `query` into a pandas DataFrame via the Snowflake cursor, then invoke
    a Python tool from the dstools catalog passing the DataFrame as `df`.

Credentials are read from environment variables (see `.env.example`). The
`snowflake-connector-python` driver is an optional dep; importing this module
without it raises a clear error only when `get_snowflake_connection()` is called.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import pandas as pd

from dstools.execution.runner import execute_tool
from dstools.registry.metadata import ToolKind
from dstools.registry.registry import get_registry

# Env-var names. Keep this list small and explicit so misconfigurations fail loudly.
_REQUIRED_ENV = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_DATABASE",
    "SNOWFLAKE_SCHEMA",
    "SNOWFLAKE_WAREHOUSE",
)
_AUTH_ENV = ("SNOWFLAKE_PASSWORD", "SNOWFLAKE_PRIVATE_KEY_PATH", "SNOWFLAKE_AUTHENTICATOR")


class MissingCredentialsError(RuntimeError):
    pass


def credentials_available() -> bool:
    """Cheap check used by pytest to skip integration tests when creds are absent."""
    if any(not os.environ.get(k) for k in _REQUIRED_ENV):
        return False
    return any(os.environ.get(k) for k in _AUTH_ENV)


def get_snowflake_connection():
    """Build a Snowflake connection from env vars. Raises a clear error
    listing what's missing rather than letting the driver fail cryptically.
    """
    try:
        import snowflake.connector  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "snowflake-connector-python is not installed. "
            "Install nexcraft-jobs with the 'snowflake' extra: "
            "pip install 'nexcraft-jobs[snowflake]'"
        ) from exc

    missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise MissingCredentialsError(
            f"Snowflake env vars missing: {missing}. See nexcraft-jobs/.env.example."
        )
    if not any(os.environ.get(k) for k in _AUTH_ENV):
        raise MissingCredentialsError(
            f"Snowflake auth env missing: set one of {list(_AUTH_ENV)}."
        )

    kwargs: dict[str, Any] = {
        "account":   os.environ["SNOWFLAKE_ACCOUNT"],
        "user":      os.environ["SNOWFLAKE_USER"],
        "database":  os.environ["SNOWFLAKE_DATABASE"],
        "schema":    os.environ["SNOWFLAKE_SCHEMA"],
        "warehouse": os.environ["SNOWFLAKE_WAREHOUSE"],
    }
    if role := os.environ.get("SNOWFLAKE_ROLE"):
        kwargs["role"] = role
    if pwd := os.environ.get("SNOWFLAKE_PASSWORD"):
        kwargs["password"] = pwd
    if pkey := os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH"):
        kwargs["private_key_file"] = pkey
        if passphrase := os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"):
            kwargs["private_key_file_pwd"] = passphrase
    if auth := os.environ.get("SNOWFLAKE_AUTHENTICATOR"):
        kwargs["authenticator"] = auth
    return snowflake.connector.connect(**kwargs)


def fetch_query(con, query: str) -> pd.DataFrame:
    """Run a query and return a pandas DataFrame."""
    cur = con.cursor()
    try:
        cur.execute(query)
        return cur.fetch_pandas_all()
    finally:
        cur.close()


def run_sql_tool_snowflake(con, name: str, params: dict[str, Any]) -> pd.DataFrame:
    """Render `name`'s template in Snowflake dialect and run it. Returns a DataFrame."""
    meta = get_registry().get(name)
    if meta.kind is not ToolKind.SQL_TEMPLATE:
        raise ValueError(f"tool {name!r} is not a SQL template (kind={meta.kind})")
    sql = execute_tool(name, params, dialect="snowflake")
    return fetch_query(con, sql)


def run_python_tool_snowflake(
    con,
    *,
    query: str,
    name: str,
    params: dict[str, Any],
    df_param: str = "df",
) -> Any:
    """Pull `query` into a DataFrame, then invoke the named Python tool with
    that DataFrame as a keyword arg (`df` by default)."""
    meta = get_registry().get(name)
    if meta.kind is not ToolKind.PYTHON:
        raise ValueError(f"tool {name!r} is not a Python tool (kind={meta.kind})")
    df = fetch_query(con, query)
    return execute_tool(name, {**params, df_param: df})


def render_only_snowflake(name: str, params: dict[str, Any]) -> str:
    """Translate a template to Snowflake SQL without executing it. Useful for
    previewing, code-review, or sending to a Snowflake worksheet by hand."""
    return execute_tool(name, params, dialect="snowflake")


def materialize_sample(
    con,
    *,
    source_table: str,
    sample_name: str = "pricemedic_sample",
    n_rows: int = 500_000,
    cast_columns: Optional[dict[str, str]] = None,
) -> str:
    """Create a session-scoped temp table holding a row-count sample of
    `source_table`. Returns the temp-table name so callers can pass it as
    `table=` to subsequent tool calls.

    Materializing once (a few seconds for ~500k rows on a small warehouse)
    keeps every downstream tool call working off the same bounded sample,
    avoiding repeated 90M-row scans of the source view.

    `cast_columns` maps column name → Snowflake cast function (e.g.,
    `"TRY_TO_TIMESTAMP"`, `"TRY_TO_NUMBER"`). Marketplace datasets often
    store dates/numbers as VARCHAR; casting at this boundary keeps every
    downstream template happy without having to encode dialect-specific
    casts into the templates.
    """
    if not source_table.replace(".", "").replace("_", "").isalnum():
        raise ValueError(f"unsafe source_table: {source_table!r}")
    if not sample_name.replace("_", "").isalnum():
        raise ValueError(f"unsafe sample_name: {sample_name!r}")
    n_rows = int(n_rows)

    casts = cast_columns or {}
    for col, fn in casts.items():
        if not col.replace("_", "").isalnum():
            raise ValueError(f"unsafe cast column: {col!r}")
        if not fn.replace("_", "").isalnum():
            raise ValueError(f"unsafe cast function: {fn!r}")

    if casts:
        excluded = ", ".join(casts)
        added = ", ".join(f"{fn}({col}) AS {col}" for col, fn in casts.items())
        projection = f"* EXCLUDE ({excluded}), {added}"
    else:
        projection = "*"

    cur = con.cursor()
    try:
        cur.execute(
            f"CREATE OR REPLACE TEMP TABLE {sample_name} AS "
            f"SELECT {projection} FROM {source_table} SAMPLE ({n_rows} ROWS)"
        )
    finally:
        cur.close()
    return sample_name
