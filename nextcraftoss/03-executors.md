# 03 — Source Executors

One module per source under `nexcraft.executors.*`. This document specifies the design for each.

## Common shape

Every executor follows the same skeleton:

```python
class XExecutor:
    kind = "x"

    async def describe(self, sql, ctx, conn):
        # 1. Validate handle kind matches
        # 2. Driver-side describe / prepare
        # 3. Translate column metadata to pa.Schema
        ...

    def execute(self, sql, ctx, conn):
        # Returns CancellableArrowStream wrapping a producer.
        # Producer is a generator that pulls from the driver and yields RecordBatches.
        async def _producer():
            ...
        return CancellableArrowStream(producer=_producer(), ctx=ctx, ...)
```

The body of `_producer` is what differs. Everything else — error translation, cancellation, budget accounting — lives in `CancellableArrowStream`.

## v0.1 source matrix

| Source     | Driver                           | Notes                                              |
|------------|----------------------------------|----------------------------------------------------|
| Postgres   | `adbc-driver-postgresql`         | Arrow-native; falls back to `asyncpg` if needed    |
| Snowflake  | `adbc-driver-snowflake`          | Arrow-native; supports `execute_partitions`        |
| Iceberg    | `datafusion-python` + `pyiceberg`| Catalog-pluggable (REST/Glue/Nessie/SQL)           |

v0.2 adds BigQuery and Delta Lake.

## Postgres executor

### Driver choice

ADBC for Postgres is preferred:

- Returns Arrow `RecordBatch` directly — no row-by-row Python conversion.
- Streams via Postgres cursor protocol; bounded memory.
- Type mapping for jsonb, arrays, intervals, numeric, timestamps with/without TZ is handled.
- One downside: ADBC's Postgres driver has slightly less mature TLS/SSL config than `asyncpg`. Most production setups still work cleanly.

### Implementation sketch

```python
import adbc_driver_postgresql.dbapi as pg_adbc
from nexcraft.core import SourceExecutor, QueryContext
from nexcraft.streaming import CancellableArrowStream
from nexcraft.errors import SourceSyntaxError, SourceRuntimeError, ConnectionError as NxConnError

class PostgresExecutor:
    kind = "postgres"

    async def describe(self, sql, ctx, conn):
        # Use prepared statement metadata
        with conn.adbc.cursor() as cur:
            try:
                cur.adbc_prepare(sql)
                schema = cur.adbc_get_table_schema(...)  # or cur.description → schema
            except pg_adbc.ProgrammingError as e:
                raise SourceSyntaxError(str(e)) from e
        return schema

    def execute(self, sql, ctx, conn):
        async def producer():
            with conn.adbc.cursor() as cur:
                try:
                    cur.execute(sql)
                except pg_adbc.ProgrammingError as e:
                    raise SourceSyntaxError(str(e)) from e
                except pg_adbc.OperationalError as e:
                    raise NxConnError(str(e)) from e

                while True:
                    if ctx.cancel.is_set():
                        # Issue real cancel via side channel
                        await self._cancel_query(conn, ctx)
                        raise CancelledError()
                    batch = cur.fetch_record_batch_chunk(ctx.batch_size_hint)
                    if batch is None or batch.num_rows == 0:
                        return
                    yield batch

        return CancellableArrowStream(producer(), ctx)

    async def _cancel_query(self, conn, ctx):
        """Issue pg_cancel_backend on a side connection."""
        # Side connection because the main one is busy with the query.
        # Connection provider exposes ctx_aux for this.
        ...
```

### Type mapping notes

ADBC handles most types correctly. Two known gotchas to document for users:

- **Aggregation type promotion**: `SUM(int_col)` returns `numeric` in Postgres but DataFusion-style consumers often expect `bigint`. Document the workaround: `CAST(SUM(int_col) AS BIGINT)` in upstream SQL. (Spice has the same caveat — not a `nexcraft` bug.)
- **Identifier case**: by default Postgres lowercases unquoted identifiers. Quoted mixed-case identifiers come back as-is. The executor preserves whatever Postgres returns; case-sensitivity is a caller concern.

### Cancellation

Postgres cancellation requires a side connection running `SELECT pg_cancel_backend(pid)`. The connection provider must expose this — either by giving the executor a separate cancel-channel function, or by reserving an aux connection on acquire. The reference `EnvVarConnectionProvider` opens a second short-lived connection to issue the cancel.

## Snowflake executor

### Driver

`adbc-driver-snowflake`. Arrow-native, supports `execute_partitions()` which is the key differentiator for throughput at TB scale.

### `execute_partitions` pattern

A normal `execute()` returns one Arrow stream. `execute_partitions()` returns N partition handles you can fetch concurrently — Snowflake-side parallelism that your client process can consume in parallel.

```python
class SnowflakeExecutor:
    kind = "snowflake"

    def execute(self, sql, ctx, conn):
        async def producer():
            with conn.adbc.cursor() as cur:
                try:
                    partitions, schema = cur.adbc_execute_partitions(sql)
                except sf_adbc.ProgrammingError as e:
                    raise SourceSyntaxError(str(e)) from e

                # Fetch partitions concurrently with bounded parallelism
                sem = asyncio.Semaphore(ctx.target_partitions)
                async def fetch_one(p):
                    async with sem:
                        sub = conn.adbc.adbc_clone()
                        with sub.cursor() as sub_cur:
                            sub_cur.adbc_read_partition(p)
                            while True:
                                batch = sub_cur.fetch_record_batch_chunk(ctx.batch_size_hint)
                                if batch is None: return
                                yield batch

                # Round-robin merge from partition fetchers
                async for batch in merge_streams(*(fetch_one(p) for p in partitions)):
                    yield batch

        return CancellableArrowStream(producer(), ctx)
```

The exact merge pattern matters less than the principle: one logical Arrow stream out, N parallel fetches under the hood.

### Cancellation

Snowflake's `SYSTEM$CANCEL_QUERY(query_id)` is the cleanest cancel. The driver exposes the active query ID after `execute*`; cache it and call cancel on a side connection when `ctx.cancel` fires.

## BigQuery executor (v0.2)

Two viable implementations:

- **ADBC** — uniform with Postgres/Snowflake. Mature enough.
- **BigQuery Storage Read API** — Google's first-class streaming API, returns Arrow IPC directly. Higher throughput for large reads. More complex auth.

Recommendation: ship ADBC in v0.2, add Storage Read API as `BigQueryStorageExecutor` (kind `bigquery_storage`) when someone needs the throughput.

## Iceberg executor

Different shape: DataFusion-native, not pass-through.

### Implementation sketch

```python
import datafusion as df
from pyiceberg.catalog import load_catalog

class IcebergExecutor:
    kind = "iceberg"

    async def describe(self, sql, ctx, conn):
        session = self._make_session(ctx)
        await self._register_tables(session, sql, conn)
        plan = session.sql(sql)
        return plan.schema()

    def execute(self, sql, ctx, conn):
        async def producer():
            session = self._make_session(ctx)
            await self._register_tables(session, sql, conn)

            try:
                plan = session.sql(sql)
            except df.SqlParseError as e:
                raise SourceSyntaxError(str(e)) from e

            stream = plan.execute_stream()
            async for batch in stream:
                if ctx.cancel.is_set():
                    return
                yield batch

        return CancellableArrowStream(producer(), ctx)

    def _make_session(self, ctx):
        cfg = df.SessionConfig().with_target_partitions(ctx.target_partitions)
        return df.SessionContext(config=cfg)

    async def _register_tables(self, session, sql, conn):
        # Inspect SQL for table refs (datafusion parser), resolve via Iceberg
        # catalog from conn, register each as a TableProvider.
        for ref in extract_table_refs(sql):
            iceberg_table = conn.catalog.load_table(ref)
            provider = iceberg_table.scan().to_arrow().to_table_provider()
            session.register_table(ref.qualified_name, provider)
```

### Pushdown

`datafusion-python` + `pyiceberg` (or `iceberg-rust` via `datafusion-iceberg`) handles:
- Manifest filtering on partition values.
- File pruning on min/max stats.
- Projection pushdown into Parquet.
- Predicate pushdown into Parquet row groups.

The executor doesn't need to do anything special for pushdown; it's automatic when the right TableProvider is registered. The executor's job is connecting the catalog and object store correctly.

### Catalog pluggability

The `IcebergConnectionHandle` carries a `pyiceberg.catalog.Catalog` instance. The connection provider configures this from source descriptor — REST catalog, Glue, Nessie, SQL — without the executor caring which.

### Per-query session

A new `SessionContext` per query. Cheap, gives tenant-isolated config and object-store credentials without global state. Avoids the trap of a long-lived `SessionContext` accumulating registered tables across tenants.

## Delta Lake executor (v0.2)

Same shape as Iceberg, using `deltalake` Python (which wraps `delta-rs`). Differences:

- Catalog is simpler — Delta tables are self-describing via their `_delta_log/`.
- Time-travel queries (`AS OF VERSION`/`TIMESTAMP`) need plumbing through to `DeltaTable.load_as_version()`.
- Predicate pushdown into Parquet works the same way.

## Adding a new executor

The intended extension path. Steps:

1. Implement `SourceExecutor` for your source.
2. Define a `ConnectionHandle` subclass carrying your driver objects.
3. Map driver exceptions to `nexcraft.errors`.
4. Register `kind` in your application code (no global registry — wiring happens at `Router` construction).
5. Run the protocol conformance suite: `pytest --pyargs nexcraft.testing.conformance --executor=mypackage.MyExecutor`.

Third-party executors are first-class. The library actively encourages this and provides the conformance suite as a reusable pytest plugin.
