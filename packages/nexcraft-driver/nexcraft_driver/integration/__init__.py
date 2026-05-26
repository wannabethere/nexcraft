"""nexcraft-jobs integration: SourceExecutors + a FedSQL factory that wires
Postgres, Snowflake, Delta-Lake, and Iceberg sources from env vars.

Used by recipes that extract through the production FedSQL path
(`recipe.extract(params, ctx, fedsql)`) rather than hitting drivers directly.
Same dstools `compute()` body runs against any source — only `source_id` changes.
"""

from nexcraft_driver.integration.source_executors import (
    AsyncpgTableExecutor,
    LakehouseExecutor,
    SnowflakeTableExecutor,
)
from nexcraft_driver.integration.fedsql_factory import (
    DELTA_SOURCE_ID,
    ICEBERG_SOURCE_ID,
    LAKEHOUSE_VIEW_NAME,
    POSTGRES_SOURCE_ID,
    SNOWFLAKE_SOURCE_ID,
    SUPPORTED_KINDS,
    build_cross_source_fedsql,
)

__all__ = [
    "AsyncpgTableExecutor",
    "LakehouseExecutor",
    "SnowflakeTableExecutor",
    "DELTA_SOURCE_ID",
    "ICEBERG_SOURCE_ID",
    "LAKEHOUSE_VIEW_NAME",
    "POSTGRES_SOURCE_ID",
    "SNOWFLAKE_SOURCE_ID",
    "SUPPORTED_KINDS",
    "build_cross_source_fedsql",
]
