# ADR 002 — Monorepo with Two Packages

**Status:** Accepted
**Date:** 2026-05

## Context

`nexcraft` is the federated SQL execution library. `nexcraft-jobs` is an opinionated analytical jobs framework that uses `nexcraft` for its extract phase. Two natural packaging questions:

1. One package or two?
2. One repo or two?

## Decision

**Two packages, one repo (monorepo).** `nexcraft` and `nexcraft-jobs`. Lockstep versioned. Apache 2.0.

## Consequences

### Why two packages

The federation primitive and the jobs framework have different audiences and different stability profiles:

- A team building a SQL-over-many-sources backend wants the executor library. They don't want Temporal as a transitive dependency.
- A team wanting analytical jobs already pulls in DuckDB, Temporal, and statistical libraries. They want the recipe runtime; the federation library comes along.

Conflating them in one package would force the federation user to install Temporal. Splitting is also good signaling: `nexcraft` is the unopinionated primitive, `nexcraft-jobs` is the opinionated framework on top. Users adopt them independently.

### Why one repo

- Lockstep evolution. Changes to `nexcraft.core.protocols` may need corresponding changes in `nexcraft-jobs`. Doing this across two repos is friction with no payoff.
- Single CI, single release pipeline, single issue tracker.
- Coherent documentation site.
- Easier for new contributors to understand the relationship.

### Lockstep versioning

Both packages release together with matching version numbers. CI enforces:

- `nexcraft-jobs/pyproject.toml` always pins `nexcraft == <same-version>` exactly.
- A release tag (`v0.1.0`) triggers both PyPI uploads.

Downstream users pinning either package end up with a coherent dependency graph automatically.

### Why not lockstep + same-package

We could ship one package with all the optional dependencies as extras (`pip install 'nexcraft[jobs]'`). Considered and rejected:

- The jobs framework is genuinely a different shape — workflows, activities, recipe registration. Squeezing it under `nexcraft.jobs.*` muddies the unopinionated executor library brand.
- Optional deps via extras work for "more sources" but get unwieldy when an extra adds a top-level concept.
- The `nexcraft` README stays clean: "this is a federation library." `nexcraft-jobs` README owns the recipe pattern story.

### Why not two repos

- Coordinating a protocol change across two repos with PR + version bump dance is friction.
- Hard to discover. New users find one repo, miss the other.
- CI duplication.

The downsides of monorepos (build complexity, cross-package noise) are minimal at this scale (two packages).

## Repo layout

See [`docs/08-repo-layout.md`](../docs/08-repo-layout.md) for full detail. Top-level shape:

```
nexcraft/                              (one git repo, one license)
├── packages/
│   ├── nexcraft/
│   └── nexcraft-jobs/
├── docs/
├── examples/
├── benchmarks/
└── .github/workflows/
```

## When this should be revisited

- If `nexcraft-jobs` grows enough that its release cadence diverges from `nexcraft` materially. Unlikely; recipes don't release independently.
- If contributor confusion becomes a real signal — e.g., people opening jobs PRs against the core package because the relationship isn't clear from the repo structure.

Both are speculative and not yet observed.
