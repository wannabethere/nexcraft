# ADR 001 — Python over Rust

**Status:** Accepted
**Date:** 2026-05
**Context:** Choosing the implementation language for `nexcraft`

## Context

The reference implementations in this space (Spice AI, wren-engine) are written in Rust. The natural question early in design was whether to follow.

Our service profile:

- Single source per query, with full pushdown.
- Live federation, no acceleration tier.
- Connection management, semantic layer, dialect translation, agent layer all in Python.
- Working set sizes in the low TB at most.
- Bottleneck (when one exists) is the source warehouse and the network, not the client process.

## Decision

`nexcraft` is implemented in Python.

## Consequences

### What we get

- **No FFI boundary.** PyO3, async runtime interop (tokio ↔ asyncio), GIL discipline, panic translation, manylinux build matrix — all costs we don't pay.
- **Same language as the rest of the platform.** Conn-mgr, dialect translator, agent layer are Python. No serialization between layers.
- **ADBC drivers cover the pass-through path natively.** `adbc-driver-postgresql`, `adbc-driver-snowflake`, `adbc-driver-bigquery` return Arrow `RecordBatch`es directly from Python with no row-by-row conversion. Python competes with Rust on this hot path.
- **`datafusion-python` covers the lakehouse path.** It's a thin wheel around the same Rust DataFusion engine Spice uses. We get Rust performance for Iceberg/Delta scans without writing or owning Rust.
- **Faster iteration.** Pure Python development cycle is materially faster than maintaining a Rust core + Python bindings.
- **Broader contributor pool.** Most of our potential OSS contributor audience is Python-fluent.

### What we don't get

- **No standalone binary.** A Python service requires a Python runtime. For embedded "drop into customer environment" deployments, this is a constraint.
- **GIL contention at high QPS.** Multi-process deployment (gunicorn workers) is the answer; some operational complexity inherits.
- **Slower in-process compute than Rust.** Mitigated by the architecture: in-process compute is minimal (the executor is glue around drivers and DataFusion); real compute happens at the source or in DataFusion (which is Rust).

## When this decision should be revisited

Specific triggers, not vibes:

1. **Profiled in-process bottleneck.** If a real production workload spends >20% wall-clock time in our Python code (not in drivers, not in DataFusion, not on the network), that's a sign the architecture's assumptions are wrong and Rust may be warranted.
2. **No good Python driver for a needed source.** Some niche source we want to support has only a C/Rust driver and writing a Python wrapper is more work than building a Rust executor.
3. **Standalone binary requirement.** A customer wants `nexcraft` as a single static binary in an environment without Python.
4. **Sustained high QPS where GIL is the bottleneck.** Not a single-customer issue; sustained across the user base.

If revisited and decided yes:

- Migration path is the Flight SQL sidecar pattern. Wrap the `SourceExecutor` protocol with a Flight client implementation backed by a Rust process. Everything else (recipes, conn-mgr, semantic layer) keeps using the Python `SourceExecutor` API.
- The protocol seam is the only thing that needs to be stable. We've designed it to be.

## Alternatives considered

### Rust core + PyO3 bindings

In-process Rust, called from Python via PyO3. Considered and rejected:

- Tokio + asyncio interop via `pyo3-asyncio` works but is fragile around cancellation and streaming.
- GIL discipline matters in hot paths; streaming Arrow batches across the boundary is exactly such a path.
- Build matrix is significant: manylinux × musllinux × macOS arm/x86 × Windows × multiple Python versions.
- Panics in Rust become opaque crashes in Python.

### Rust sidecar with Flight SQL

A separate Rust process serving Flight SQL, Python clients connect over localhost. This is the *future* migration path if/when triggers fire — not the v0.1 plan.

### Pure Rust

Considered and rejected because the rest of our platform is Python and the federation layer is not the hot path that justifies the boundary.

## Notes

This decision is deliberately conservative. The cost of getting it wrong in one direction (Python now, migrate to Rust later) is well-understood — design the protocol seam carefully, take the migration when needed. The cost of getting it wrong the other direction (build Rust core + bindings now) is large up-front engineering on a problem we don't yet have.
