"""nexcraft-driver: Flight-SQL gRPC driver fronting nexcraft sources.

Two surfaces:
  • `nexcraft_driver.integration`  — SourceExecutors + FedSQL factory (moved
    here from `nexcraft_jobs.integration`; the driver and its executors live
    together, recipes layer on top).
  • `nexcraft_driver.server`       — Flight gRPC server exposing sync SQL plus
    SubmitQuery / GetQueryStatus / FetchQueryResults / CancelQuery actions.

The server is a thin shell over the same `Router` + `FedSQLClient` used by
recipes; switching from in-process to Temporal-backed async query state is a
single `AsyncQueryStore` impl swap.
"""

from nexcraft_driver.integration import (
    AsyncpgTableExecutor,
    DELTA_SOURCE_ID,
    ICEBERG_SOURCE_ID,
    LAKEHOUSE_VIEW_NAME,
    LakehouseExecutor,
    POSTGRES_SOURCE_ID,
    SNOWFLAKE_SOURCE_ID,
    SUPPORTED_KINDS,
    SnowflakeTableExecutor,
    build_cross_source_fedsql,
)

__all__ = [
    "AsyncpgTableExecutor",
    "DELTA_SOURCE_ID",
    "ICEBERG_SOURCE_ID",
    "LAKEHOUSE_VIEW_NAME",
    "LakehouseExecutor",
    "POSTGRES_SOURCE_ID",
    "SNOWFLAKE_SOURCE_ID",
    "SUPPORTED_KINDS",
    "SnowflakeTableExecutor",
    "build_cross_source_fedsql",
]
