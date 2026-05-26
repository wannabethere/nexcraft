"""DuckDB UDFs aligned with ``genieml/insightsagents/app/tools/mltools`` and ``sql_functions.json``."""

from nexcraft_jobs.compute.udfs.mltools.invoke_sql_function import register_invoke_sql_udfs
from nexcraft_jobs.compute.udfs.mltools.list_helpers import register_list_helpers_udfs
from nexcraft_jobs.compute.udfs.mltools.registry import register_mltools_udfs

__all__ = [
    "register_invoke_sql_udfs",
    "register_list_helpers_udfs",
    "register_mltools_udfs",
]
