# Jobs 04 — Result Storage

Results live in two places: bulk data in Parquet on object storage, metadata rows in Postgres. This document specifies the layout, the `ResultStore` interface, and the lifecycle.

## Why two stores

- **Object storage** — durable, cheap, scales horizontally, queryable from DuckDB / Spark / DataFusion / anything. Right home for tabular results.
- **Postgres** — transactional, indexable, queryable by metadata (recipe, params, status, time range). Right home for the index over results.

Tightly coupling result data to a relational schema would be wrong (results are tabular but the shape varies per recipe). Storing metadata only in object storage would be wrong (no efficient "list jobs by tenant for the last 7 days").

## `ResultStore` interface

```python
from typing import Protocol
from dataclasses import dataclass
import pyarrow as pa

@dataclass
class ResultRef:
    """Pointer to a persisted result. Returned by store.write(); resolved by store.read()."""
    job_id: str
    primary_uri: str                    # s3://bucket/jobs/{job_id}/primary.parquet
    auxiliary_uris: dict[str, str]      # name -> uri
    metadata_uri: str                   # s3://bucket/jobs/{job_id}/metadata.json
    schema_json: str
    row_count: int
    bytes: int
    created_at: datetime

class ResultStore(Protocol):
    async def write(
        self,
        job_id: str,
        primary: pa.Table,
        auxiliaries: dict[str, pa.Table] | None = None,
        metadata: dict | None = None,
        params: dict | None = None,
    ) -> ResultRef: ...

    async def read(self, ref: ResultRef) -> pa.Table:
        """Loads the primary table from object storage."""

    async def read_auxiliary(self, ref: ResultRef, name: str) -> pa.Table: ...

    async def get(self, job_id: str) -> ResultRef:
        """Looks up a result ref by job_id from the metadata DB."""

    async def list(
        self,
        tenant_id: str,
        recipe_name: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[ResultRef]: ...

    async def delete(self, job_id: str) -> None:
        """Soft-delete in metadata; actual object deletion is a sweeper job."""
```

## Object storage layout

```
s3://nexcraft-results/
└── jobs/
    └── {tenant_id}/
        └── {recipe_name}/
            └── {date}/
                └── {job_id}/
                    ├── primary.parquet
                    ├── aux/
                    │   ├── by_region.parquet
                    │   └── summary.parquet
                    ├── metadata.json
                    └── params.json
```

### Layout rationale

- **Tenant-prefix.** Enables IAM policies for tenant isolation (S3 prefix policies, GCS ACLs).
- **Recipe-prefix.** Easy "all variance results last quarter" listings.
- **Date-prefix.** Lifecycle policies (delete after N days) work cleanly on date prefixes.
- **Job-id leaf.** All artifacts of one job in one place; trivial cleanup.

### Parquet specifics

- Column statistics enabled (min/max/null_count) — supports pushdown when results are queried later.
- Row group size: 128 MB target (DuckDB-friendly).
- Compression: ZSTD level 3 (good ratio, fast decompression).
- Schema: written as Parquet schema; original Arrow schema preserved in `metadata.json` for fidelity (Arrow → Parquet is lossy for some types).

## Metadata DB

A single Postgres table backs job metadata. Keep it simple; resist the urge to model recipe-specific data here.

```sql
CREATE TABLE job_runs (
    job_id          TEXT PRIMARY KEY,        -- ULID
    tenant_id       TEXT NOT NULL,
    recipe_name     TEXT NOT NULL,
    recipe_version  TEXT NOT NULL,
    workflow_id     TEXT,                    -- Temporal workflow ID
    status          TEXT NOT NULL,           -- queued|running|succeeded|failed|cancelled
    submitted_at    TIMESTAMPTZ NOT NULL,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    duration_ms     BIGINT,

    params          JSONB NOT NULL,          -- input parameters
    metadata        JSONB,                   -- recipe-emitted summary

    primary_uri     TEXT,
    auxiliary_uris  JSONB,                   -- {name: uri}
    metadata_uri    TEXT,                    -- the metadata.json on object store
    schema_json     TEXT,
    row_count       BIGINT,
    bytes           BIGINT,

    error_class     TEXT,                    -- on failure
    error_message   TEXT,                    -- on failure (sanitized)

    deleted         BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at      TIMESTAMPTZ,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_job_runs_tenant_recipe_time
    ON job_runs (tenant_id, recipe_name, submitted_at DESC)
    WHERE NOT deleted;

CREATE INDEX idx_job_runs_workflow ON job_runs (workflow_id);

CREATE INDEX idx_job_runs_status_submitted
    ON job_runs (status, submitted_at)
    WHERE status IN ('queued', 'running');

-- Optional: for params filtering ("find all variance jobs for region X")
CREATE INDEX idx_job_runs_params_gin ON job_runs USING gin (params jsonb_path_ops);
```

### Status transitions

```
queued ─▶ running ─▶ succeeded
   │         │
   │         ├──▶ failed
   │         │
   │         └──▶ cancelled
   │
   └────────────▶ cancelled (cancelled before start)
```

The Temporal workflow updates status via the persist activity (`succeeded`) and via failure handlers (`failed`, `cancelled`).

## Reference implementation: `ParquetObjectStore`

```python
import pyarrow.parquet as pq
import pyarrow.fs as fs
import json
from sqlalchemy.ext.asyncio import AsyncEngine

class ParquetObjectStore:
    def __init__(self, fs: fs.FileSystem, base_uri: str, engine: AsyncEngine):
        self._fs = fs
        self._base = base_uri
        self._db = engine

    async def write(self, job_id, primary, auxiliaries=None, metadata=None, params=None) -> ResultRef:
        # 1. Object storage
        run = await self._lookup_run(job_id)            # tenant, recipe, etc.
        prefix = self._prefix(run)
        primary_uri = f"{prefix}/primary.parquet"
        await self._write_parquet(primary, primary_uri)

        aux_uris = {}
        if auxiliaries:
            for name, table in auxiliaries.items():
                uri = f"{prefix}/aux/{name}.parquet"
                await self._write_parquet(table, uri)
                aux_uris[name] = uri

        meta_uri = f"{prefix}/metadata.json"
        await self._write_json(meta_uri, {
            "schema": primary.schema.to_string(),
            "metadata": metadata or {},
            "auxiliaries": list(aux_uris.keys()),
        })
        params_uri = f"{prefix}/params.json"
        await self._write_json(params_uri, params or {})

        # 2. Metadata DB
        ref = ResultRef(
            job_id=job_id,
            primary_uri=primary_uri,
            auxiliary_uris=aux_uris,
            metadata_uri=meta_uri,
            schema_json=primary.schema.to_string(),
            row_count=primary.num_rows,
            bytes=primary.nbytes,
            created_at=datetime.now(UTC),
        )
        await self._update_run_metadata(job_id, ref, metadata=metadata)
        return ref

    async def read(self, ref: ResultRef) -> pa.Table:
        return pq.read_table(ref.primary_uri, filesystem=self._fs)

    # ...
```

## Reading results

Three patterns callers use:

### Direct read

```python
result = await store.read(ref)
# result is a pa.Table; convert to pandas/polars as needed
```

### Streaming read (large results)

```python
async for batch in store.read_streaming(ref):
    # batch is a pa.RecordBatch
    process(batch)
```

### Querying via DuckDB

The most powerful pattern — point DuckDB at the Parquet directly:

```python
con.execute(f"""
    SELECT region, AVG(variance_pct) AS avg_variance
    FROM read_parquet('{ref.primary_uri}')
    WHERE period > '2024-01-01'
    GROUP BY region
""")
```

DuckDB pushes the predicate into Parquet, reads only what it needs from object storage. The results layout is essentially a small data lake. Apps build dashboards on top of this directly.

## Lifecycle and retention

- **TTL.** Default 90 days. Configurable per tenant or per recipe.
- **Deletion.** Soft-delete via `deleted=true` in the metadata table. A nightly sweeper job deletes the underlying objects after a grace period (default 7 days).
- **Failed jobs.** Metadata kept for the same TTL as successful jobs; staging Parquet from extract phase kept 7 days for post-mortem.

The sweeper is a separate Temporal workflow (`scheduled_cleanup`) — uses the same infrastructure, runs once daily.

## Why this design rather than "store results in Postgres"

A common alternative is to store small results directly as JSON or array columns in Postgres. Reasons against:

- Result sizes are unpredictable; recipes that legitimately produce 100M rows blow up Postgres.
- Postgres is the system of record for *metadata*, not data. Conflating the two creates pressure to add recipe-specific columns ("the variance recipe needs an `attribution` column") and grows toward an unwieldy schema.
- Object storage + DuckDB is a more honest "small data warehouse" pattern. Apps that want SQL-over-results get it for free.

The line between metadata and data is enforced by the schema: `metadata` JSONB column is for *summary* (row counts, regions covered, key stats) — typically <1 KB. Anything larger goes to Parquet.
