# 14 — Policy Enforcement (RLS / CLS)

Where row-level and column-level security live relative to the driver, and what the architectural choice implies for defense in depth.

## Position

Three principles, in priority order:

1. **The driver does not enforce policy.** It receives SQL, dispatches it to an executor, and streams Arrow back. Whatever predicates or projections the SQL contains, the source sees as written. The driver is the transport, not the gate.
2. **The policy gate sits upstream of the driver.** Whoever produces the SQL — an LLM-driven agent, a BI gateway, a notebook proxy — owns RLS and CLS application. They consume the policy definitions from the dataservices `/protection` API and rewrite SQL accordingly before calling `submit()`.
3. **Defense in depth lives in the source.** Warehouse sources (Postgres, Snowflake) support native row-access and masking policies. When the policy layer fails open or is bypassed, the source still enforces.

## Why this split

The alternative is to bake a policy engine into `nexcraft-driver` (SQLGlot AST rewrite, fetch policies from dataservices, apply per query). That works and is portable across all four sources. We chose not to for three reasons:

- **Single producer of SQL.** In our deployment, agents own SQL generation end-to-end. The LLM has the most context about the query intent, the user session, and which session properties apply. Re-rewriting the same SQL inside the driver duplicates logic.
- **Driver stays small.** `nexcraft-driver` is a thin gRPC shell over `FedSQLClient`. Pulling in dataservices client code, policy caching, AST rewriting, and the test surface for all of it is a multiplier on the package's complexity and dependencies.
- **Cross-cutting concerns belong to the gateway, not the protocol.** Authn, authz, rate limiting, audit log, and policy enforcement are all gateway-layer concerns. The driver is the protocol layer.

The trade-off: **100% of the security boundary sits on the gateway/agent layer**. If a debug script, internal tool, or misconfigured agent submits SQL without going through the policy rewriter, the source returns whatever the SQL asks for.

## What dataservices owns

The dataservices `/protection` API (documented in [`dataservices/docs/DATA_PROTECTION_API.md`](../../genieml/dataservices/docs/DATA_PROTECTION_API.md)) is the single source of truth for policy definitions:

- **RLS** (`rls_policies`) — predicate templates per `model_ref`, parameterised by named session properties (`:tenant_id`, `:role`).
- **CLS** (`cls_policies`) — protected columns gated on a single session property check (`in`, `equals`, `not_in`).
- **Inheritance** — org-level defaults plus per-connection overrides, merged at read time.

The gateway/agent layer is expected to:

1. Fetch the effective policy set for the target `connection_id` and `organization_id`: `GET /protection/connections/{id}/effective`.
2. Apply RLS predicates and CLS projections to the generated SQL, binding `:placeholder` values from the request session.
3. Submit the rewritten SQL via the FlightSQL driver.

How the gateway applies policies is not the driver's problem. SQLGlot-based AST rewriting is the obvious approach (parse, walk table references, wrap each with a guarded subquery), but any equivalent approach — view substitution, query templating, hand-built rewriters — is fine.

## Defense in depth: source-native policies

Source-native enforcement is the only thing that protects against bypass of the gateway. It works on warehouse sources and not on lakehouse sources, so it's partial coverage — but it's the partial coverage that matters most, because warehouse sources hold the bulk of regulated data in most deployments.

| Source | Native enforcement | Notes |
|---|---|---|
| **Postgres** | `CREATE POLICY` on each table, `ALTER TABLE … ENABLE ROW LEVEL SECURITY`. Predicates use `current_setting('app.tenant_id')` etc. | Driver connection must `SET app.tenant_id = '…'` per query. Snowflake-like inheritance via roles. |
| **Snowflake** | Row access policies (`CREATE ROW ACCESS POLICY`) and masking policies (`CREATE MASKING POLICY`), attached to tables/columns. Predicates reference `CURRENT_ROLE()` and session variables. | Driver sets session variables with `ALTER SESSION` before query. Policies apply automatically to every SELECT, regardless of who wrote the SQL. |
| **Delta Lake** | None natively. Enforcement happens in whichever engine reads the table. | DuckDB-via-extension has no row-access concept; rely on gateway rewrite. |
| **Iceberg** | None natively in the format. Catalog implementations (Unity, Glue) increasingly support views with masking, but coverage is inconsistent. | Rely on gateway rewrite plus catalog-level visibility filtering. |

The dataservices Data Protection API already holds the policy definitions; a follow-on workstream is generating the source-native DDL from those definitions and installing it. That stays out of the driver — it's an operator-time concern handled by a sync job (recipe, cron, or ad-hoc script) reading from `/protection/connections/{id}/effective` and emitting `CREATE POLICY` / `CREATE ROW ACCESS POLICY` against the configured source.

## What this means operationally

| Concern | Owner | Mechanism |
|---|---|---|
| Decide what's protected | Org admin via `/protection/orgs/{id}/config` | Policy CRUD in dataservices |
| Apply policies at query time | Gateway / agent | SQL rewrite before driver submit |
| Block bypass on warehouse sources | Operations | DDL synced from dataservices to source |
| Block bypass on lakehouse sources | Gateway / agent only | No source-native fallback |
| Audit which policies fired | Gateway | Log policy IDs alongside SQL before submit |
| Test policy correctness | Gateway test suite | Snapshot rewritten SQL per (policy, session) |

The driver participates in none of these. It runs SQL.

## What this section in the driver does NOT do

- Parse SQL for policy violation detection.
- Reject SQL that references restricted columns.
- Filter results by row predicate after the fact.
- Mask column values in-stream.
- Read from the dataservices `/protection` API.
- Maintain a policy cache.

If any of these are needed, they land in a separate `nexcraft-policy` package or in the gateway service. The driver remains protocol-only.

## When to revisit

The driver-as-transport choice is reversible. Move policy enforcement into the driver when **any** of the following becomes true:

- Multiple SQL producers exist for the same driver instance, and they cannot be trusted to apply policies uniformly.
- A breach or audit finding traces to a bypass of the gateway rewriter.
- Source-native enforcement for the warehouse sources has been deployed and the lakehouse-only gap becomes the operator's largest open risk.

In any of those cases, adding a `PolicyEngine` middleware to the driver is roughly 500 lines plus tests. The interfaces (`SubmitRequest`, executor protocol, session context) accommodate it without breaking changes — the work is the engine itself, not the wiring.

## Cross-references

- [`dataservices/docs/DATA_PROTECTION_API.md`](../../genieml/dataservices/docs/DATA_PROTECTION_API.md) — the policy definition CRUD surface
- [`12-bi-integration.md`](12-bi-integration.md) — BI tools sit upstream of the driver, same as agents; if they ever submit SQL directly, the same gateway/policy responsibility applies
- [`13-long-running-queries.md`](13-long-running-queries.md) — the async submission path has the same policy posture: rewrite at submit time, not at execute time
