# Nexcraft — Design Documents

Federated SQL execution and analytical jobs for Python.

This repository contains the design specification for two OSS packages:

- **`nexcraft`** — a federated SQL execution library. BYO dialect-correct SQL, get Arrow streaming results back across warehouses, RDBMSes, and lakehouses through a single executor protocol.
- **`nexcraft-jobs`** — an optional analytical jobs framework on top of `nexcraft`, using Temporal as the workflow runtime and DuckDB as the compute engine for variance, trend, cohort, and what-if analyses.

The two packages live in one monorepo, version in lockstep, and ship under Apache 2.0.

## What this is and isn't

**Is:** an executor layer. You hand it dialect-correct physical SQL targeted at one source, plus a `QueryContext`. It runs the SQL, streams Arrow back, honors cancellation and budgets, and gets out of the way.

**Isn't:** a query planner, a semantic layer, a SQL generator, a dialect translator, or a cross-source query optimizer. Those are upstream concerns. Cross-source compute is a downstream concern (`nexcraft-jobs` recipes handle it via DuckDB pipelines).

This narrow scope is the point. Existing tools (`datafusion-python`, `ibis`, `connectorx`, `pyiceberg`, `deltalake`) each cover a slice; none provides a single executor protocol for "run this dialect SQL on that source and stream Arrow back."

## Reading order

1. [`docs/00-vision.md`](docs/00-vision.md) — what we're building and why
2. [`docs/01-architecture.md`](docs/01-architecture.md) — the architecture, two execution paths
3. [`docs/02-protocols.md`](docs/02-protocols.md) — the protocol surface (the API contract)
4. [`docs/03-executors.md`](docs/03-executors.md) — per-source executor designs
5. [`docs/04-streaming.md`](docs/04-streaming.md) — streaming, cancellation, budgets
6. [`docs/05-servers.md`](docs/05-servers.md) — optional Flight SQL and HTTP servers
7. [`docs/06-observability.md`](docs/06-observability.md) — OTel, metrics, structured logs
8. [`docs/07-testing.md`](docs/07-testing.md) — testing strategy and conformance suite
9. [`docs/08-repo-layout.md`](docs/08-repo-layout.md) — repository and package layout
10. [`docs/09-security.md`](docs/09-security.md) — RLS, CLS, table access, audit logging
11. [`jobs/00-overview.md`](jobs/00-overview.md) — `nexcraft-jobs` overview
12. [`jobs/01-recipes.md`](jobs/01-recipes.md) — the recipe pattern
13. [`jobs/02-temporal.md`](jobs/02-temporal.md) — Temporal runtime design
14. [`jobs/03-duckdb-udfs.md`](jobs/03-duckdb-udfs.md) — DuckDB compute layer and analytical UDFs
15. [`jobs/04-storage.md`](jobs/04-storage.md) — result persistence

## Architectural decisions

Recorded as ADRs in [`decisions/`](decisions/).

- [001 — Python over Rust](decisions/001-python-over-rust.md)
- [002 — Monorepo with two packages](decisions/002-monorepo-two-packages.md)
- [003 — Temporal as the jobs runtime](decisions/003-temporal-for-jobs.md)
- [004 — Single source per query](decisions/004-single-source-per-query.md)
