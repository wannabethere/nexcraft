"""Run dstools tools against PostgreSQL.

Mirror of `snowflake_runner.py` for the Postgres path:

- `get_postgres_connection()`  — env-var driven; raises a clear MissingCredentialsError
  listing what's missing instead of letting psycopg fail cryptically.
- `materialize_sample(con, source_table, sample_name, n_rows, cast_columns)`
  — `CREATE TEMP TABLE … AS SELECT … FROM source TABLESAMPLE BERNOULLI (p)` /
  `LIMIT n` so downstream tool calls run against a small bounded slice.
- `run_sql_tool_postgres(con, name, params)` — render with `dialect="postgres"`,
  execute, return pandas DataFrame.
- `run_python_tool_postgres(con, query, name, params)` — pull a DataFrame and
  feed it as `df` to a Python tool.

The `psycopg2` driver is an optional extra; importing this module without it
is fine — only `get_postgres_connection()` raises.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import pandas as pd

from dstools.execution.runner import execute_tool
from dstools.registry.metadata import ToolKind
from dstools.registry.registry import get_registry

_REQUIRED_ENV = (
    "POSTGRES_HOST",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
)


class MissingCredentialsError(RuntimeError):
    pass


def credentials_available() -> bool:
    return all(os.environ.get(k) for k in _REQUIRED_ENV)


def get_postgres_connection():
    try:
        import psycopg2  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "psycopg2 is not installed. Install nexcraft-jobs with the "
            "'postgres' extra: pip install 'nexcraft-jobs[postgres]'"
        ) from exc

    missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise MissingCredentialsError(
            f"Postgres env vars missing: {missing}. See .env.example."
        )

    # Strip: .env / shell export often leaves trailing newline or spaces, which
    # breaks Azure (and other) password checks while still "looking" correct in the editor.
    host = os.environ["POSTGRES_HOST"].strip()
    dbname = os.environ["POSTGRES_DB"].strip()
    user = os.environ["POSTGRES_USER"].strip()
    password = os.environ["POSTGRES_PASSWORD"].strip()

    return psycopg2.connect(
        host=host,
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        dbname=dbname,
        user=user,
        password=password,
        sslmode=os.environ.get("POSTGRES_SSL_MODE", "prefer"),
        # The Azure Postgres flexible server requires this; harmless elsewhere.
        connect_timeout=int(os.environ.get("POSTGRES_CONNECT_TIMEOUT", "30")),
    )


def fetch_query(con, query: str, params: Optional[tuple] = None) -> pd.DataFrame:
    """Run a query and return a pandas DataFrame. Column names are taken
    verbatim from psycopg2 (lowercased for unquoted identifiers, case-preserved
    for quoted ones)."""
    cur = con.cursor()
    try:
        cur.execute(query, params or ())
        if cur.description is None:
            return pd.DataFrame()
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return pd.DataFrame(rows, columns=cols)
    finally:
        cur.close()


def run_sql_tool_postgres(con, name: str, params: dict[str, Any]) -> pd.DataFrame:
    """Render `name`'s template in Postgres dialect and run it. Returns a DataFrame."""
    meta = get_registry().get(name)
    if meta.kind is not ToolKind.SQL_TEMPLATE:
        raise ValueError(f"tool {name!r} is not a SQL template (kind={meta.kind})")
    sql = execute_tool(name, params, dialect="postgres")
    return fetch_query(con, sql)


def run_python_tool_postgres(
    con,
    *,
    query: str,
    name: str,
    params: dict[str, Any],
    df_param: str = "df",
) -> Any:
    meta = get_registry().get(name)
    if meta.kind is not ToolKind.PYTHON:
        raise ValueError(f"tool {name!r} is not a Python tool (kind={meta.kind})")
    df = fetch_query(con, query)
    return execute_tool(name, {**params, df_param: df})


def render_only_postgres(name: str, params: dict[str, Any]) -> str:
    """Translate a template to Postgres SQL without executing it."""
    return execute_tool(name, params, dialect="postgres")


def materialize_sample(
    con,
    *,
    source_table: str,
    sample_name: str = "cornerstone_sample",
    n_rows: int = 200_000,
    cast_columns: Optional[dict[str, str]] = None,
) -> str:
    """Create a session-scoped TEMP TABLE holding a bounded slice of
    `source_table`. Returns the temp-table name so callers can pass it as
    `table=` to subsequent tool calls.

    Uses `TABLESAMPLE BERNOULLI (p)` with `p` sized to target ~n_rows from the
    underlying row count (rough — Bernoulli is probabilistic), then `LIMIT n_rows`
    to bound the slice exactly. Acceptable for analytics demos; not deterministic
    across sessions.

    `cast_columns` is a {column: cast_function} map (e.g., `{"effective_date":
    "TIMESTAMP"}` would emit `CAST(effective_date AS TIMESTAMP)`). Use Postgres-
    style casts here, not function names — Postgres has no TRY_TO_TIMESTAMP.
    """
    # source_table is allowed to contain dots and (quoted) camelCase; the rest
    # of the inputs must be plain identifiers.
    _validate_pg_identifier(source_table, allow_dot_and_quote=True, label="source_table")
    _validate_pg_identifier(sample_name, allow_dot_and_quote=False, label="sample_name")
    n_rows = int(n_rows)

    casts = cast_columns or {}
    for col, target in casts.items():
        _validate_pg_identifier(col, allow_dot_and_quote=True, label=f"cast key {col!r}")
        if not target.replace(" ", "").replace("_", "").replace("(", "").replace(")", "").isalnum():
            raise ValueError(f"unsafe cast target: {target!r}")

    # Always build an explicit projection so every column lands in the temp
    # table with a lowercase alias. Downstream env-var-driven references
    # (e.g., CORNERSTONE_SCORE_COL=score) then work as plain identifiers
    # without shell-quoting gymnastics.
    select_parts = _build_select_projection(con, source_table, casts)
    quoted_source = _pg_quote_identifier(source_table)

    cur = con.cursor()
    try:
        cur.execute(f'DROP TABLE IF EXISTS "{sample_name}"')
        cur.execute(
            f'CREATE TEMP TABLE "{sample_name}" AS '
            f"SELECT {select_parts} FROM {quoted_source} LIMIT {n_rows}"
        )
        con.commit()
    finally:
        cur.close()
    return sample_name


# ---------------------------------------------------------------------------

def _validate_pg_identifier(value: str, *, allow_dot_and_quote: bool, label: str) -> None:
    """Reuse dstools' identifier regex when applicable; otherwise stricter."""
    from dstools.sql.engine import _IDENT_RE  # type: ignore[attr-defined]
    if allow_dot_and_quote:
        if not _IDENT_RE.match(value):
            raise ValueError(f"unsafe {label}: {value!r}")
        return
    if not value.replace("_", "").isalnum():
        raise ValueError(f"unsafe {label}: {value!r}")


def _pg_quote_identifier(name: str) -> str:
    """Quote each dot-separated part of `name` for Postgres. Parts already
    wrapped in `"..."` are left as-is. Forcing the quoted form makes the
    SQL we emit case-preserving regardless of how the user spelled the input
    in their `.env` (Postgres folds unquoted identifiers to lowercase, so
    `csod_learn_datamodel.Transcript_csod` without quotes looks up
    `transcript_csod` — and breaks)."""
    parts = []
    for part in name.split("."):
        if part.startswith('"') and part.endswith('"') and len(part) >= 2:
            parts.append(part)
        else:
            parts.append(f'"{part}"')
    return ".".join(parts)


def _build_select_projection(con, source_table: str, casts: dict[str, str]) -> str:
    """Explicit projection that aliases every source column to its lowercased
    name in the temp table, and applies any requested CAST to selected columns.
    Postgres has no `SELECT * EXCLUDE`, so we enumerate columns via
    information_schema."""
    schema, table = _split_schema_table(source_table)
    cur = con.cursor()
    try:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (schema, table),
        )
        cols = [r[0] for r in cur.fetchall()]
    finally:
        cur.close()
    if not cols:
        raise RuntimeError(
            f"information_schema returned no columns for {source_table!r}. "
            f"Resolved to schema={schema!r}, table={table!r}. Check spelling, "
            f"case, and visibility to the connected user."
        )

    # Map for cast lookups: information_schema gives the on-disk column name
    # (case-preserved when the column was created quoted). Match against
    # whatever the user passed in `casts`, ignoring quotes and case.
    cast_targets = {c.strip('"').lower(): t for c, t in casts.items()}

    pieces = []
    for c in cols:
        alias = _safe_lower_identifier(c)
        if c.lower() in cast_targets:
            target = cast_targets[c.lower()]
            pieces.append(f'CAST("{c}" AS {target}) AS {alias}')
        else:
            pieces.append(f'"{c}" AS {alias}')
    return ", ".join(pieces)


def _safe_lower_identifier(name: str) -> str:
    """Lowercase a column name. If lowercasing would still leave non-identifier
    chars (shouldn't, since names came from information_schema), fall back to
    the quoted form."""
    lc = name.lower()
    if lc.replace("_", "").isalnum() and (lc[0].isalpha() or lc[0] == "_"):
        return lc
    return f'"{lc}"'


def _split_schema_table(name: str) -> tuple[str, str]:
    """Split `schema.table` or `db.schema.table` into (schema, table). Strips
    surrounding quotes from each part before returning."""
    parts = name.split(".")
    if len(parts) == 1:
        return ("public", parts[0].strip('"'))
    if len(parts) == 2:
        return (parts[0].strip('"'), parts[1].strip('"'))
    # 3-part: catalog.schema.table → use schema, table; catalog is metadata
    return (parts[1].strip('"'), parts[2].strip('"'))
