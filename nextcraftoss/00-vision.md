# 00 — Vision and Scope

## Problem statement

Modern analytical workloads pull data from many sources: operational Postgres, cloud warehouses (Snowflake, BigQuery), and lakehouse formats (Iceberg, Delta) on object storage. The Python ecosystem has good *single-source* libraries (`adbc-driver-postgresql`, `pyiceberg`, `deltalake`, `datafusion-python`) and good *query builders* (`ibis`, `sqlalchemy`), but no clean executor layer that:

1. Takes dialect-correct SQL produced upstream (by an agent, by sqlglot, by dbt, or hand-written).
2. Resolves it to a source via a pluggable catalog.
3. Streams Arrow `RecordBatch`es back end-to-end.
4. Honors cancellation, deadlines, and memory/row budgets.
5. Treats every source the same through one protocol.

`nexcraft` fills that gap.

## Pitch

> I already have my SQL. Just run it cleanly across any source and give me Arrow back, with cancellation and budgets that actually work.

## Two execution paths, one protocol

Sources fall into two categories with genuinely different semantics. The protocol unifies them; the implementations don't pretend to be the same shape.

### Pass-through executors
Postgres, Snowflake, BigQuery, MySQL, MSSQL — sources that *are* SQL engines. The executor sends the dialect SQL to the source, the source plans and runs it, the executor wraps the result cursor as an Arrow stream. DataFusion is not in the picture.

### DataFusion-native executors
Iceberg, Delta, raw Parquet on object storage — formats, not engines. The executor uses `datafusion-python` as the engine, registers the format-specific `TableProvider`, and lets DataFusion plan and execute the query with predicate/projection pushdown into the scan layer.

Both implementations satisfy the same `SourceExecutor` protocol and return the same `AsyncIterator[pa.RecordBatch]`. Callers don't need to know or care which path is in use.

## Non-goals

Explicitly *not* in scope for `nexcraft`:

- **Query planning across sources.** Cross-source joins are not supported and not on the roadmap. If you need them, materialize via `nexcraft-jobs` and join in DuckDB.
- **Dialect translation.** Bring your own dialect-correct SQL. `sqlglot`, `datafusion-python.unparse`, dbt, hand-written — your choice.
- **Semantic modeling.** No MDL, no metric layer, no entity model. Use Cube, dbt-metricflow, your own — or none.
- **Text-to-SQL.** Upstream concern. Bring your own agent.
- **Caching / acceleration tiers.** Spice's differentiator. Out of scope here. If users want it, they build it on top.
- **Connection pool management.** A `ConnectionProvider` protocol is required; the reference implementation is minimal. Real production deployments plug in their own (vault-backed credentials, multi-tenant pooling, etc.).

The narrow scope is deliberate. Each thing it doesn't do is a thing it doesn't have to argue with.

## Audience

- **Application teams** embedding federated execution in Python services, MCP servers, agents, or analytics backends.
- **Data platform teams** wanting a thin, predictable executor under their own semantic layer or query builder.
- **Researchers / notebook users** who want a single API that works against Postgres, Snowflake, and Iceberg without juggling four libraries.

## Versioning posture

`nexcraft.core` (the protocol surface) is the only stable API in v0.1. Everything else is experimental and may change. This is communicated in the README and enforced in CI.

`v1.0` ships when:

- Three reference executors (Postgres, Snowflake, Iceberg) are stable.
- Conformance suite passes for all three.
- Public TPC-H benchmark results are reproducible.
- API surface in `nexcraft.core` has had no breaking changes for two minor releases.
