# 01 — Architecture

## High-level

```
┌─────────────────────────────────────────────────────────────────┐
│                  Caller (agent, app, notebook)                  │
│        Already has dialect-correct SQL + source_id              │
└───────────────────────────────┬─────────────────────────────────┘
                                │  client.execute(source_id, sql, ctx)
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                       FedSQLClient                              │
│   Public API. Thin wrapper over Router.                         │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                          Router                                 │
│   1. Catalog.get_source(source_id) → SourceDescriptor           │
│   2. ConnectionProvider.acquire(source_id, ctx) → handle        │
│   3. dispatch to executor by SourceKind                         │
└─────┬─────────────────────────────────────┬─────────────────────┘
      │                                     │
      ▼                                     ▼
┌──────────────────────┐           ┌──────────────────────┐
│  Pass-through path   │           │  DataFusion path     │
│  (warehouses/RDBMS)  │           │  (lakehouse formats) │
│                      │           │                      │
│  PostgresExecutor    │           │  IcebergExecutor     │
│  SnowflakeExecutor   │           │  DeltaExecutor       │
│  BigQueryExecutor    │           │                      │
│                      │           │  datafusion-python   │
│  ADBC drivers        │           │  + TableProvider     │
│                      │           │  + pushdown          │
└──────────┬───────────┘           └──────────┬───────────┘
           │                                  │
           └──────────────┬───────────────────┘
                          ▼
              CancellableArrowStream
                (bounded queue, deadline, cancel,
                 row/byte budget enforcement)
                          │
                          ▼
            AsyncIterator[pa.RecordBatch]
                          │
                          ▼
                   back to caller
```

The two execution paths are deliberately separate. They share the protocol, the streaming primitive, the router, and the gateway — nothing else.

## Layer model

```
┌───────────────────────────────────────────────────────────────┐
│ 6 │ Optional servers   Flight SQL  •  HTTP/REST               │
├───────────────────────────────────────────────────────────────┤
│ 5 │ Public API         FedSQLClient  •  Router                │
├───────────────────────────────────────────────────────────────┤
│ 4 │ Source executors   Postgres • Snowflake • BQ • Iceberg…   │
├───────────────────────────────────────────────────────────────┤
│ 3 │ Streaming          CancellableArrowStream • budgets       │
├───────────────────────────────────────────────────────────────┤
│ 2 │ Pluggables         Catalog • ConnectionProvider           │
├───────────────────────────────────────────────────────────────┤
│ 1 │ Core protocols     SourceExecutor • QueryContext • errors │
└───────────────────────────────────────────────────────────────┘
```

Each layer depends only on layers below. Layer 1 has zero non-stdlib dependencies — it's the integration surface and stays tiny.

## Component responsibilities

### Layer 1 — Core protocols (`nexcraft.core`)
Defines `SourceExecutor`, `Catalog`, `ConnectionProvider`, `QueryContext`, `SourceDescriptor`, `ConnectionHandle`, and the typed error hierarchy. Pure protocols and dataclasses. No I/O. This is the only fully stable surface in v0.1.

### Layer 2 — Pluggables (`nexcraft.catalog`, `nexcraft.connection`)
Reference implementations of the catalog and connection-provider protocols. Production users plug their own in.

- `InMemoryCatalog`, `YAMLCatalog` for development and tests.
- `EnvVarConnectionProvider`, `StaticConnectionProvider` for development.
- The protocols are the API; the impls are *examples*.

### Layer 3 — Streaming (`nexcraft.streaming`)
The `CancellableArrowStream` primitive. Wraps a producer (driver cursor, DataFusion stream) into a bounded async `AsyncIterator[pa.RecordBatch]`. Single source of truth for backpressure, cancellation propagation, deadline enforcement, and budget accounting. Centralized so bugs get fixed once.

### Layer 4 — Source executors (`nexcraft.executors`)
One module per source. Each implements `SourceExecutor`. Pass-through executors use ADBC where possible. Lakehouse executors use `datafusion-python` and the relevant format library. Executors are *thin*: connect, execute, wrap in `CancellableArrowStream`, return.

### Layer 5 — Public API (`nexcraft.client`, `nexcraft.router`)
`Router` does source resolution and executor dispatch. `FedSQLClient` is a thin facade over the router with convenience methods (`execute`, `execute_to_table`, `describe`).

### Layer 6 — Optional servers (`nexcraft.server.flight`, `nexcraft.server.http`)
Both run on top of the same `Router`. Flight SQL is the recommended primary; HTTP is for apps and debugging. Default deployment is "embed as library" — servers are opt-in extras.

## Why two paths

The single most important shape decision in this architecture is keeping pass-through and DataFusion-native executors visibly separate.

**Reasons:**

- *Different semantics.* Pass-through delegates planning to the source. DataFusion-native plans locally with pushdown into the scan. Forcing a unified abstraction over these papers over real differences and creates leaky abstractions.
- *Different dependency graphs.* Postgres-only deployments shouldn't pull in `iceberg-rust`'s avro/parquet transitive deps. Extras-based optional installs (`pip install 'nexcraft[postgres]'`) keep this clean.
- *Different failure modes.* Pass-through fails when the source fails (network, syntax, types). DataFusion-native fails when the file format is bad, the catalog is stale, or pushdown couldn't reduce a scan. Different errors, different remediation.
- *Different performance characteristics.* Pass-through latency is dominated by the source. DataFusion-native latency is dominated by metadata and scan. Different observability, different tuning.

The protocol unifies the *call shape*. The executors don't pretend to be the same thing.

## Where Path 1 (`nexcraft`) and Path 2 (`nexcraft-jobs`) meet

```
┌──────────────────────────────────────────────────────────┐
│                    nexcraft-jobs                         │
│  ┌────────────────────────────────────────────────────┐  │
│  │ Recipe                                             │  │
│  │   extract → stage → compute → persist              │  │
│  └────────────────────────────────────────────────────┘  │
│             │                                            │
│             │  uses FedSQLClient for extract phase       │
└─────────────┼────────────────────────────────────────────┘
              ▼
┌──────────────────────────────────────────────────────────┐
│                       nexcraft                           │
│              (federation execution layer)                │
└──────────────────────────────────────────────────────────┘
```

`nexcraft-jobs` recipes call `nexcraft` for their extract phase. The federation service doesn't know whether the caller is an interactive agent or a Temporal worker. Same client, same `SourceExecutor`, same Arrow streams.

This is architecturally important: there is no separate "batch federation" path. The same federation primitive serves both, the difference is only in who's calling and what budgets they set.

## What's in scope per layer

| Layer | In scope | Out of scope |
|-------|----------|--------------|
| Core protocols | Stability, minimalism, zero deps | Convenience helpers |
| Pluggables | Reference impls, well-documented protocols | Production-grade catalogs/conn-mgrs |
| Streaming | Backpressure, cancellation, budgets, deadlines | Caching, materialization |
| Executors | Per-source connect/execute/stream | Cross-source joins, dialect translation |
| Public API | Router dispatch, client facade | Query optimization |
| Servers | Flight SQL, HTTP — both thin | Auth backends, RBAC, multi-tenant routing |
