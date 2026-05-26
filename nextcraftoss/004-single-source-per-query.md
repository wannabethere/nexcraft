# ADR 004 — Single Source per Query

**Status:** Accepted
**Date:** 2026-05

## Context

A federated SQL service has to take a stand on cross-source queries: SQL that references tables across multiple physical sources (e.g., joining a Postgres `users` table with a Snowflake `orders` table in one statement).

Two camps exist:

- **Cross-source federation.** The system plans the query, splits it into per-source subqueries, executes them, and joins results in a coordinator engine. Trino, Presto, Dremio, parts of Spice live here.
- **Single-source-per-query.** Every query targets exactly one physical source. Cross-source compute is the caller's problem (or a separate batch layer's problem).

This is one of the two or three decisions that shape the entire architecture.

## Decision

`nexcraft` is **single-source-per-query**. The router resolves `source_id` → executor → connection. The SQL is sent to that one source; results stream back. Cross-source queries are out of scope.

Cross-source compute is handled in `nexcraft-jobs` recipes via DuckDB pipelines: extract from each source separately (multiple `nexcraft` queries, each single-source), stage the streams, join in DuckDB.

## Consequences

### Why single-source

- **Full pushdown.** When the SQL goes to the source, the source's query planner — Postgres's, Snowflake's, BigQuery's — does what it does best. Predicate pushdown, projection pushdown, partition pruning, optimizer costing on real statistics. We don't second-guess any of it.
- **Predictable latency.** No coordinator-side hash join blowing up. Latency is "what the source takes," plus marginal overhead for streaming.
- **No coordinator engine to operate.** Trino is fantastic and operationally heavy. We don't ship one. Our process footprint is small; we scale by adding more lightweight workers.
- **Errors stay local.** A query failure has one cause, at one source, with one error message. No "the join coordinator gave up after one of three subqueries failed" debugging.
- **Auth and governance stay at the source.** RLS, column-level security, audit logs — all enforced by the source the user already has policies for. We don't have to model those policies in a federation layer.

### What we give up

- **Real-time cross-source joins.** A user who wants `SELECT u.email, o.total FROM postgres.users u JOIN snowflake.orders o ON u.id = o.user_id LIMIT 10` in one synchronous request — they don't get it from `nexcraft`.
- **Cross-source query optimization.** No bushy-join planner across heterogeneous sources. We don't try.

### Why this is acceptable

The empirical pattern in real analytical work:

1. Most queries against one source are inherently single-source. "Show me last week's revenue" lives in the warehouse; "show me operational user state" lives in the OLTP DB. Crossing them is rare.
2. When users *do* need cross-source results, they almost always want freshness measured in minutes/hours, not seconds. That's a recipe / scheduled-job pattern, not an interactive-federation pattern.
3. The cases that really need real-time cross-source joins are usually solved upstream — by replicating one source into the other (CDC into the warehouse), not by a federation layer doing fan-out joins on the hot path.

For the cases that really do need synchronous cross-source: route to a heavier system (Trino, Dremio). `nexcraft` is honest about not being one of those.

### Cross-source via recipes

The recipe path covers the legitimate "I need data from two sources joined":

```python
class CrossSourceCohortRecipe:
    async def extract(self, params, ctx, fedsql):
        return {
            "users":  await fedsql.execute_to_reader(
                          "prod_pg",     "SELECT id, email, signup_date FROM users", ...),
            "orders": await fedsql.execute_to_reader(
                          "warehouse",   "SELECT user_id, total FROM orders WHERE ...", ...),
        }

    async def compute(self, inputs, params, ctx):
        con = ctx._duckdb
        # Both registered as DuckDB tables by the runtime.
        return ComputeResult(primary=con.execute("""
            SELECT u.email, SUM(o.total) AS lifetime_value
            FROM users u JOIN orders o ON u.id = o.user_id
            GROUP BY u.email
        """).arrow())
```

Same federation primitive, just called twice. Joins happen in DuckDB, which is an honest cross-source compute engine for this scale.

### What this simplifies

A startling amount of the architecture comes for free from this decision:

- The `Router` is straightforward — `(source_id, ctx) → executor.execute(sql, ctx, conn)`. No query plan splitting, no result coordination, no distributed retry.
- Connection management is per-query, per-source. No long-lived federation sessions to track.
- Authorization is per-source. The `ConnectionProvider` resolves credentials for `(tenant, source)`; the source enforces what the user can see. We don't model permissions ourselves.
- Observability is per-query, per-source. Spans have a single source attribution. Metrics partition cleanly by `kind`.

Trying to be multi-source in v0.1 would have meant doing all of this at the coordinator layer. That's an entire product on its own.

## Alternatives considered

### Cross-source federation via DataFusion as coordinator

Use `datafusion-python` as the coordinator. Each source is a `TableProvider` that translates DataFusion subqueries into source SQL. Joins happen in DataFusion.

This is essentially what Spice does. It's a real, working architecture. We rejected it because:

- Ownership of dialect translation moves to us. `sqlglot` is good but cross-source plans expose its edges.
- Cardinality estimation is heroic — we'd need source statistics in DataFusion's catalog, kept fresh, per source.
- The performance profile is hard to reason about. Sometimes great, sometimes catastrophic, with thin abstractions hiding which.
- Not the job we're trying to do. Recipes solve this for the cases that need it, with explicit data movement and predictable cost.

### Limited cross-source: only "pull from source X, scan in DataFusion"

A weaker form: queries can read from one warehouse source via DataFusion if the query is simple enough. Considered and rejected — adds protocol complexity for no clear use case the recipe pattern doesn't already cover better.

## When this should be revisited

- If the recipe pattern proves insufficient for a real and common cross-source need that genuinely requires interactive latency. Hasn't happened yet; would need to actually happen, not be hypothetical.
- If Spice or another OSS project produces a cross-source federation library so good that we can adopt it as a higher layer on top of `nexcraft`'s single-source primitive.

In neither case does the single-source decision in v0.1 need to be undone. `nexcraft` stays the executor; cross-source becomes a separate layer.
