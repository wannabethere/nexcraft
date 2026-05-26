# 02 — Core Protocols

The protocol surface is the API. Everything else in `nexcraft` is implementation detail. This document defines the contracts every executor and pluggable must honor.

All protocols and core types live under `nexcraft.core`. This module has no non-stdlib runtime dependencies except `pyarrow`.

## `SourceExecutor`

The single integration point. Third-party executors implement this protocol and become first-class citizens.

```python
from typing import Protocol, AsyncIterator, runtime_checkable
import pyarrow as pa

@runtime_checkable
class SourceExecutor(Protocol):
    """Executes dialect-correct SQL against a single source and streams Arrow."""

    @property
    def kind(self) -> str:
        """Stable identifier for the source kind. e.g. 'postgres', 'iceberg'."""

    async def describe(
        self,
        sql: str,
        ctx: "QueryContext",
        conn: "ConnectionHandle",
    ) -> pa.Schema:
        """Returns the result schema for the SQL without executing it."""

    def execute(
        self,
        sql: str,
        ctx: "QueryContext",
        conn: "ConnectionHandle",
    ) -> AsyncIterator[pa.RecordBatch]:
        """Executes the SQL and yields RecordBatches.

        MUST honor ctx.cancel and ctx.deadline.
        MUST raise nexcraft.errors.* exceptions, not driver-specific ones.
        SHOULD respect ctx.max_rows / ctx.max_bytes if set.
        """
```

### Contract details

- **`kind`** — stable, lowercase, snake_case. Used for routing, observability tags, and error attribution. Reserved kinds: `postgres`, `mysql`, `mssql`, `snowflake`, `bigquery`, `redshift`, `iceberg`, `delta`, `parquet`, `duckdb`, `sqlite`, `clickhouse`, `trino`. Third parties should namespace: `acme:custom_warehouse`.
- **`describe`** — must be cheap. Pass-through executors use `PREPARE` or driver-side describe. DataFusion-native executors plan but don't execute.
- **`execute`** — async iterator. The first batch may be slow (planning, network); subsequent batches stream. The iterator MUST close cleanly on cancellation and propagate cancellation to the source.
- **No retries.** Executors don't retry. Retries are a caller concern with caller-known idempotency semantics.
- **No mutation.** v0.1 is read-only. `INSERT`/`UPDATE`/`DELETE`/`COPY` are not supported semantics; if the source happens to execute them, it's the caller's problem.

## `QueryContext`

```python
from dataclasses import dataclass, field
from datetime import datetime
import asyncio
from typing import Optional

@dataclass(frozen=True)
class QueryContext:
    # Identity & tracing
    tenant_id: str
    query_id: str                       # ULID, propagated end-to-end
    trace_id: Optional[str] = None      # OTel trace context
    parent_span_id: Optional[str] = None

    # Lifecycle
    deadline: Optional[datetime] = None             # absolute, not relative
    cancel: asyncio.Event = field(default_factory=asyncio.Event)

    # Budgets
    max_rows: Optional[int] = None
    max_bytes: Optional[int] = None

    # Execution hints (advisory)
    target_partitions: int = 4          # used by DataFusion path; ignored by pass-through
    batch_size_hint: int = 8192         # rows per RecordBatch

    # Tags for observability / governance
    tags: tuple[tuple[str, str], ...] = ()
```

### Contract details

- **`tenant_id`** — required, non-empty. Even single-tenant deployments pass a constant value (`"default"`). This forces the abstraction; users who add multi-tenancy later don't refactor every callsite.
- **`query_id`** — required. ULID preferred for time-ordering in logs.
- **`deadline`** — absolute timestamp. Executors compare to `datetime.now(timezone.utc)`. Relative deadlines are caller-side concerns.
- **`cancel`** — `asyncio.Event` set by anyone with the context. Executors poll it between batches and *also* drive a real source-side cancel (`pg_cancel_backend`, Snowflake `SYSTEM$CANCEL_QUERY`, etc.) — not just task cancellation, which would leave ghost queries.
- **Frozen** — context is immutable. To change something, derive a new one with `dataclasses.replace`.

## `Catalog`

```python
@runtime_checkable
class Catalog(Protocol):
    async def get_source(self, source_id: str) -> "SourceDescriptor": ...
    async def list_sources(self, tenant_id: str | None = None) -> list["SourceDescriptor"]: ...

@dataclass(frozen=True)
class SourceDescriptor:
    source_id: str             # opaque, caller-meaningful
    kind: str                  # matches an executor's kind
    display_name: str
    tenant_id: str
    config: dict               # source-specific (host, dataset, warehouse, etc.)
    tags: dict[str, str] = field(default_factory=dict)
```

The catalog answers "what is `source_id`?" — not "what tables does it have?" Schema discovery is a separate concern (and not in scope for v0.1; many sources expose their own information_schema).

## `ConnectionProvider`

```python
@runtime_checkable
class ConnectionProvider(Protocol):
    async def acquire(
        self,
        source_id: str,
        ctx: QueryContext,
    ) -> "ConnectionHandle":
        """Returns a handle valid for the duration of one query.

        The provider owns the underlying pool and credential lifecycle.
        Implementations resolve credentials based on (source_id, tenant_id).
        """

@dataclass
class ConnectionHandle:
    """Source-specific connection wrapper. Returned by the provider, consumed by the executor.

    Subclasses carry the actual driver objects:
      - PostgresConnectionHandle has an asyncpg or adbc connection
      - SnowflakeConnectionHandle has a snowflake-connector-python session
      - IcebergConnectionHandle has a catalog + object store + table identifier
      - etc.

    Lifecycle is managed by the provider, not the executor. The executor
    must NOT close the underlying connection — only release the handle
    (typically via async context manager).
    """
    source_id: str
    kind: str
```

### Why a typed handle, not a generic object

The executor needs to know it has the right kind of connection before using it. A generic `Any` works, but loses type safety and produces obscure errors when the wrong provider is wired up. Each executor pairs with a specific handle subclass; the router validates `handle.kind == executor.kind` before dispatch.

### Credentials never appear in `QueryContext`

This is a hard rule. Credentials live with the provider; the context carries identity (`tenant_id`) only. The provider resolves credentials from the (tenant, source) pair internally. This keeps audit-sensitive material out of every log line that prints a context.

## Error taxonomy

Defined in `nexcraft.errors`. All executors raise from this hierarchy.

```python
class NexcraftError(Exception):
    """Base. All library errors derive from this."""

class TimeoutError(NexcraftError):
    """Deadline exceeded."""

class CancelledError(NexcraftError):
    """ctx.cancel was set or caller cancelled."""

class ConnectionError(NexcraftError):
    """Could not acquire a connection or connection was lost mid-query."""

class AuthenticationError(ConnectionError):
    """Credentials rejected by source."""

class SourceSyntaxError(NexcraftError):
    """Source rejected the SQL with a syntax error.

    Carries source's original error message in `source_message`.
    """
    source_message: str

class SourceRuntimeError(NexcraftError):
    """Source executed the SQL but failed at runtime (type error, missing table, etc.)."""
    source_message: str

class SchemaMismatchError(NexcraftError):
    """Result schema didn't match the schema returned by describe()."""

class BudgetExceededError(NexcraftError):
    """max_rows or max_bytes exceeded mid-stream."""
    budget_kind: str           # "rows" | "bytes"
    limit: int
    observed: int

class ConfigurationError(NexcraftError):
    """Source descriptor or connection config was invalid."""

class InternalError(NexcraftError):
    """Bug in nexcraft itself. Open an issue."""
```

Executors *must* translate driver-specific exceptions into this hierarchy. The original exception is preserved as `__cause__` (use `raise NexcraftError(...) from e`). Callers should never catch driver-specific exceptions — that's the leak `nexcraft` exists to fix.

## Stability commitments

- `nexcraft.core` — strict SemVer. Breaking changes only on major version bumps. No additions to `Protocol` definitions in minor releases (additions are technically breaking under structural typing).
- `nexcraft.errors` — error class names and hierarchy stable; messages are not stable.
- `nexcraft.executors`, `nexcraft.streaming`, `nexcraft.router`, `nexcraft.client`, `nexcraft.server.*` — best-effort stability with deprecation warnings, but breakage possible in minor releases until v1.0.

The README and PyPI metadata document this clearly. Users who want maximum stability program against `nexcraft.core` only.
