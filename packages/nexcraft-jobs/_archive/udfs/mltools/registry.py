from __future__ import annotations

import duckdb

from nexcraft_jobs.compute.udfs.mltools.invoke_sql_function import register_invoke_sql_udfs
from nexcraft_jobs.compute.udfs.mltools.list_helpers import register_list_helpers_udfs


def register_mltools_udfs(con: duckdb.DuckDBPyConnection) -> None:
    """List helpers (columnar → JSON ``p_data``) plus ``invoke_sql_function`` catalog dispatch."""
    register_list_helpers_udfs(con)
    register_invoke_sql_udfs(con)
