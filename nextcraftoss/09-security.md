# 09 — Security: RLS, CLS, and Access Control

`nexcraft` is a **policy transport**, not a policy engine. Sources already have first-class enforcement (Postgres RLS, Snowflake row access policies, BigQuery policy tags, Iceberg view ACLs). Our job is to carry the right identity to the source so those policies fire correctly, gate which sources a tenant can reach, and produce an audit trail.

This document specifies the trust boundaries, the protocol additions that make the security story explicit, the three viable credential-to-principal mapping patterns, and the per-source enforcement points.

## Position

Three principles, in priority order:

1. **Enforcement happens at the source.** Row filters, column masks, table grants — all source-side. We don't parse SQL to enforce them.
2. **`nexcraft` enforces source visibility per tenant.** The catalog is an authorization decision: "can this tenant even ask about source X?"
3. **Audit at our layer is non-negotiable.** Every query produces a structured audit record. The source's audit log is the truth, but `nexcraft` records what *it* saw — tenant assertion, principal binding, outcome — independent of the source.

Everything else falls out of these.

## Trust boundaries

```
┌──────────────────────────────────────────────────────────────┐
│ Caller (agent, app, BI tool)                                 │
│ Untrusted w.r.t. enforcement                                 │
└────────────────────────┬─────────────────────────────────────┘
                         │ SQL + asserted tenant_id (from JWT/mTLS)
                         ▼
┌──────────────────────────────────────────────────────────────┐
│ nexcraft                                                     │
│ Trusts: tenant_id assertion from validated auth              │
│ Enforces: source visibility per tenant (Catalog)             │
│           "can this tenant ask about source X?"              │
└────────────────────────┬─────────────────────────────────────┘
                         │ (tenant_id, source_id) → handle
                         ▼
┌──────────────────────────────────────────────────────────────┐
│ ConnectionProvider                                           │
│ Trusts: vault / IAM for credential resolution                │
│ Enforces: principal mapping                                  │
│           "this tenant maps to that source-side principal"   │
└────────────────────────┬─────────────────────────────────────┘
                         │ Connection authenticated as principal P
                         ▼
┌──────────────────────────────────────────────────────────────┐
│ Source                                                       │
│ Enforces: RLS, CLS, table GRANTs, masking — all of it        │
│ Based on: principal P's identity in the source               │
└──────────────────────────────────────────────────────────────┘
```

The single thing `nexcraft` accepts as truth from above is the asserted `tenant_id`. The auth layer (JWT validator, mTLS peer cert verifier) is what produces this assertion; `nexcraft` does not validate tokens itself.

## Why not enforce in `nexcraft`

Recurring temptation: parse the SQL, rewrite it, inject row filters. We don't.

- The caller hands us dialect-correct SQL across Postgres, Snowflake, BigQuery, DataFusion. Parsing all of those reliably is `sqlglot`'s job, upstream — and even sqlglot doesn't promise round-trip fidelity on every construct.
- Sources have mature policy engines we can't outdo. Postgres RLS has had a decade of hardening; Snowflake's row access policies integrate with their query planner; BigQuery's policy tags participate in column lineage. We'd be reinventing all of this, badly.
- Pushdown stays clean — the source planner treats the policy as part of its own plan and pushes it through the optimizer.
- Audit teams want enforcement *at the data*. That's where the strongest audit story lives.
- A policy engine in `nexcraft` becomes a security boundary. Bugs become CVEs.

What `nexcraft` does enforce: source visibility (catalog) and principal binding (provider). Both are simple, both are testable.

## Three credential→principal mapping patterns

The `ConnectionProvider` is where tenant identity becomes source identity. Three viable patterns; pick per source based on the security model you can actually operate.

### Pattern A — Service account + role assumption per request

Single service account holds the connection; role switches per query.

```python
class RoleAssumingPostgresProvider:
    """One pooled service account. Per-query SET ROLE binds the principal."""

    def __init__(self, pool, tenant_to_role: Callable[[str], str]):
        self._pool = pool
        self._tenant_to_role = tenant_to_role

    async def acquire(self, source_id, ctx):
        conn = await self._pool.acquire()
        principal = self._tenant_to_role(ctx.tenant_id)   # tenant_42 → "app_user_42"
        # SET LOCAL is critical — scoped to the transaction, doesn't leak between queries
        await conn.execute(f"SET LOCAL ROLE {self._quote_ident(principal)}")
        return PostgresConnectionHandle(
            adbc=conn,
            source_id=source_id,
            kind="postgres",
            principal=principal,
            principal_kind="role",
        )

    async def release(self, handle):
        # RESET ROLE before returning to pool — defense in depth
        await handle.adbc.execute("RESET ROLE")
        await self._pool.release(handle.adbc)
```

**Source RLS shape** (Postgres example):

```sql
CREATE POLICY tenant_isolation ON orders
  USING (assigned_role = current_user);
```

**Trade-offs:**

- ✓ One pool, one credential to manage.
- ✓ Source sees the right principal in `current_user`.
- ✗ Requires `SET LOCAL ROLE` discipline. Forgetting `LOCAL` leaks across pooled connections.
- ✗ Not all sources support clean role switching mid-session.

Best fit: Postgres, Snowflake (`USE SECONDARY ROLES`), MySQL (`SET ROLE`).

### Pattern B — Per-tenant credentials

Provider holds a different credential per tenant. The connection authenticates as the actual tenant principal.

```python
class PerTenantSnowflakeProvider:
    """Each tenant has its own Snowflake user/role. Vault-backed."""

    def __init__(self, vault: VaultClient):
        self._vault = vault
        self._pools: dict[tuple[str, str], ConnectionPool] = {}

    async def acquire(self, source_id, ctx):
        key = (source_id, ctx.tenant_id)
        if key not in self._pools:
            creds = await self._vault.get(f"snowflake/{source_id}/{ctx.tenant_id}")
            self._pools[key] = self._make_pool(creds)
        conn = await self._pools[key].acquire()
        return SnowflakeConnectionHandle(
            adbc=conn,
            source_id=source_id,
            kind="snowflake",
            principal=conn.user,
            principal_kind="user",
        )
```

**Trade-offs:**

- ✓ Cleanest enforcement — source sees real user identity in audit logs.
- ✓ No session-state leakage risk; each pool is per-tenant.
- ✗ Credential lifecycle per tenant. Rotation, revocation, monitoring all multiply.
- ✗ Pool count scales with tenant count — connection-cost ceiling.

Best fit: Snowflake (federated SSO with SCIM-provisioned tenant users), BigQuery (per-tenant service accounts via Workload Identity Federation), regulated environments where audit logs must show real users.

### Pattern C — Service account + session context variable

Single service account; pass identity as a session variable; RLS policies reference it.

```python
class SessionContextPostgresProvider:
    """One pooled service account. Per-query session variable carries identity."""

    async def acquire(self, source_id, ctx):
        conn = await self._pool.acquire()
        # is_local=true → scoped to the transaction
        await conn.execute(
            "SELECT set_config('app.tenant_id', $1, true), "
            "       set_config('app.user_id',   $2, true)",
            ctx.tenant_id, self._user_for(ctx),
        )
        return PostgresConnectionHandle(
            adbc=conn,
            source_id=source_id,
            kind="postgres",
            principal=f"svc_nexcraft@tenant={ctx.tenant_id}",
            principal_kind="session_context",
        )
```

**Source RLS shape:**

```sql
CREATE POLICY tenant_isolation ON orders
  USING (tenant_id = current_setting('app.tenant_id')::int);
```

**Trade-offs:**

- ✓ One pool, one credential.
- ✓ RLS policies are simple SQL referencing `current_setting`.
- ✗ Identity propagation is implicit — easy to write a policy that forgets to check `app.tenant_id`.
- ✗ Source-side audit log shows the service account, not the tenant. Audit must come from `nexcraft`.

Best fit: Postgres-heavy applications where RLS rules are co-designed with the application schema.

### Choosing

| Concern | Pattern A | Pattern B | Pattern C |
|---|---|---|---|
| Credential lifecycle | Single | Per-tenant | Single |
| Pool count | One | N (tenants) | One |
| Source-side audit identity | Real role | Real user | Service account |
| RLS policy complexity | Standard `current_user` | Standard `current_user` | `current_setting` |
| Risk of identity leakage | Medium (SET LOCAL discipline) | Low | Medium (variable not set) |

Default recommendation in docs: **Pattern A** for Postgres/Snowflake when role-switching is supported; **Pattern B** for regulated tenants where source-side audit must show the real user.

All three implement the same `ConnectionProvider` protocol. `nexcraft` doesn't care which.

## Row-level security

Pure source-side. `nexcraft` ensures the connection authenticates as the right principal; the source's row-filter policies fire automatically.

| Source | Mechanism | What `nexcraft` does |
|---|---|---|
| Postgres | `CREATE POLICY ... USING (...)`, RLS enabled per table | Connect as right role (Pattern A) or set session variable (Pattern C) |
| Snowflake | Row Access Policies on tables and views | Connect with right role/secondary roles |
| BigQuery | Authorized views; or RLS via `SESSION_USER()` checks | Service account with delegated identity, or per-tenant SA |
| Iceberg | View-based — expose filtered views; hide base tables via catalog ACLs | Catalog returns the per-tenant view as the table reference |
| Delta | Same view-based pattern as Iceberg | Same as Iceberg |
| MySQL | Views with `DEFINER`/`INVOKER` security; no first-class RLS | Per-tenant views named in catalog |

For Iceberg/Delta specifically: the connection handle carries a `pyiceberg.catalog.Catalog` instance scoped per tenant. Different tenants get different catalog clients with different namespace ACLs. The catalog returns view-tables instead of base tables for tenants that should only see filtered subsets. Catalog-layer enforcement is the equivalent of source-side RLS for lakehouse formats.

## Column-level security

Same shape — source enforces. Result Arrow batches arrive already masked, or already missing the columns the principal can't see.

| Source | Mechanism |
|---|---|
| Postgres | Column-level `GRANT SELECT (col1, col2) ON ...`. `SELECT denied_col` errors at the source. |
| Snowflake | Dynamic data masking + tag-based masking. Masked values returned as `NULL`, hashed, or transformed per policy. |
| BigQuery | Policy tags via Data Catalog. Unauthorized access produces a permission error. |
| Iceberg / Delta | View-based — define views that project only allowed columns; hide base columns. |

If `SELECT *` returns fewer columns than the SQL implies, that's the source telling us which ones the principal sees. We don't second-guess.

### Optional: paranoid column allowlist

For shops that want a cross-check independent of the source's enforcement — defense in depth:

```python
class PolicyAwareRouter(Router):
    """Optional: post-describe column allowlist check.

    Off by default. Enable when an external policy store is the system of record
    for column visibility and you want belt-and-suspenders enforcement.
    """

    def __init__(self, *args, policy: ColumnPolicy | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._policy = policy

    async def execute(self, source_id, sql, ctx):
        executor, conn = await self._resolve(source_id, ctx)
        if self._policy is not None:
            schema = await executor.describe(sql, ctx, conn)
            allowed = await self._policy.column_allowlist(ctx.tenant_id, source_id)
            if allowed is not None:
                forbidden = set(schema.names) - allowed
                if forbidden:
                    raise PolicyViolationError(
                        f"columns not allowed for tenant {ctx.tenant_id}: {sorted(forbidden)}"
                    )
        return executor.execute(sql, ctx, conn)
```

The `ColumnPolicy` is whatever the user provides — Open Policy Agent, an internal service, a YAML file. `nexcraft` doesn't ship one; the integration point is well-defined.

## Table-level access control

Two layers, both real:

### Layer 1 — `nexcraft` source visibility

The `Catalog.get_source(source_id, tenant_id)` returns only sources the tenant can ask about. Asking for a source the tenant can't see returns `SourceNotFoundError` — same error as if it didn't exist. **No information leak about its existence.**

```python
class Catalog(Protocol):
    async def get_source(self, source_id: str, tenant_id: str) -> SourceDescriptor:
        """Raises SourceNotFoundError if the tenant cannot reach source_id.

        Returning the source descriptor is itself an authorization decision.
        Implementations MUST NOT distinguish 'doesn't exist' from 'forbidden'
        in the error path — both produce SourceNotFoundError.
        """

    async def list_sources(self, tenant_id: str) -> list[SourceDescriptor]:
        """Returns only sources visible to the tenant."""
```

The reference `YAMLCatalog` enforces this from a per-tenant `visible_sources` allowlist. Production deployments plug in their own catalog backed by the org's source-of-truth (LDAP groups, IAM, internal IDP).

### Layer 2 — within-source table grants

Once the principal connects, the source's `GRANT` tree decides which tables they can read. `SELECT FROM hidden_table` returns the source's permission-denied error, which the executor translates to `SourceRuntimeError`.

The catalog isn't reinventing source-level grants; it's gating source-id reachability. Fine-grained "this tenant can read tables A, B but not C in source X" lives in the source's GRANT tree, not in our catalog. We deliberately don't model that — it's a layer of complexity that always drifts from the source's reality.

## Protocol additions for security

Two small extensions to the core protocols make the security story explicit. These are documented as extensions here; folding into [`02-protocols.md`](02-protocols.md) is a follow-up.

### `principal` on `ConnectionHandle`

```python
@dataclass
class ConnectionHandle:
    source_id: str
    kind: str
    principal: str | None = None         # source-side identity used for this query
    principal_kind: str | None = None    # 'role' | 'user' | 'service_account' |
                                         # 'session_context' | 'delegated' | None
```

Every connection provider populates `principal`. The audit logger reads it. Operators tracing "which source-side identity ran this query?" find it without grepping driver internals.

### `tenant_id` on `Catalog.get_source`

The current protocol has `list_sources(tenant_id)` but `get_source(source_id)` is tenant-blind. Make `get_source` tenant-scoped:

```python
class Catalog(Protocol):
    async def get_source(self, source_id: str, tenant_id: str) -> SourceDescriptor: ...
    async def list_sources(self, tenant_id: str) -> list[SourceDescriptor]: ...
```

Single-tenant deployments pass a constant `"default"` and never see the divergence. Multi-tenant deployments enforce visibility on every lookup, not just listing.

### Reserved principal kinds

A small reserved enum, like `kind` for executors. Third parties can namespace (`acme:saml_assertion`):

| Value | Meaning |
|---|---|
| `role` | Connected as a role assumed via `SET ROLE` or equivalent |
| `user` | Connected as a real user account |
| `service_account` | Connected as a service principal (no per-user identity) |
| `session_context` | Service account with identity passed via session variable |
| `delegated` | Connected via OBO/impersonation flow (e.g., BigQuery Workload Identity) |

## Audit logging

Non-negotiable. Every query produces a structured audit record. This is what makes the security story defensible to a SOC2 auditor.

### Event shape

```python
{
  "event": "nexcraft.query",
  "schema_version": "1",
  "query_id": "01HXAB...",                # ULID, time-ordered
  "trace_id": "...",                      # OTel trace context

  # Identity (asserted vs bound)
  "tenant_id": "tenant_42",               # asserted by auth layer, validated upstream
  "asserted_subject": "user-alice@acme",  # raw subject from JWT/cert (if available)
  "source_id": "prod_pg",
  "source_kind": "postgres",
  "principal": "app_user_42",             # from ConnectionHandle.principal
  "principal_kind": "role",

  # Outcome
  "outcome": "success",                   # success|cancelled|denied|timeout|
                                          # budget_exceeded|source_error|connection_error
  "deadline_exceeded": false,
  "rows_returned": 1234,
  "bytes_returned": 98765,
  "duration_ms": 412,
  "first_batch_ms": 67,

  # When things go wrong
  "error_class": null,                    # e.g. "SourceSyntaxError"
  "error_message": null,                  # source-emitted, may be redacted

  # Optional, opt-in per source
  "sql_redacted": null,                   # SQL with literals replaced by ?

  # Context propagation
  "tags": {"caller": "agent-7", "purpose": "dashboard"},

  "timestamp": "2026-05-07T18:00:00Z",
  "host": "nexcraft-worker-3.us-east-1",
}
```

### Sink

Routed to a separate audit sink in production, not just the application log. Implementations:

- **Default:** structured stderr, intended for collection by the host log pipeline (Fluent Bit, Vector, Datadog, etc.) and routing to an audit-only index.
- **Opt-in:** direct emit to a dedicated audit destination (Kafka topic, S3 prefix, Splunk HEC) via a pluggable `AuditSink` protocol.

The audit sink is independent of the observability sink. Application logs may be retained 30 days; audit logs may need 7 years. Different lifecycles, different access controls, separate sinks.

### What's deliberately not in the audit record

- **Raw SQL by default.** May contain values that are themselves data (`WHERE ssn = '123-45-6789'`). Off by default; opt-in per source via `audit_log_sql: true` with a literal-redaction pass.
- **Result data.** Schemas only, never values.
- **Connection credentials.** Never.
- **Session state.** Whatever variables the provider set is implementation detail.

### Redaction pass

When `audit_log_sql: true`, a sqlglot-based pass replaces literal values with placeholders before logging:

```
SELECT * FROM users WHERE email = 'alice@acme.com' AND age > 21
→
SELECT * FROM users WHERE email = ? AND age > ?
```

Imperfect — sqlglot can miss exotic constructs — but acceptable as defense in depth. If the operator can't accept "good enough," they leave SQL logging off and rely on the source's own audit.

## What this means for `nexcraft-jobs`

Recipes extract through `nexcraft`, so source policies apply during extract automatically. The Temporal workflow's `JobContext.tenant_id` flows into every `QueryContext`; the `ConnectionProvider` resolves the right principal; the source filters.

Two enforcement points specific to jobs:

### Result storage prefix isolation

Persisted Parquet lives at `s3://bucket/jobs/{tenant_id}/{recipe_name}/{date}/{job_id}/`. IAM policies on the bucket enforce per-tenant prefix access independently of the metadata DB:

```json
{
  "Statement": [{
    "Effect": "Allow",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::nexcraft-results/jobs/${aws:PrincipalTag/tenant_id}/*"
  }]
}
```

Even if the metadata DB had a bug, object storage can't be cross-tenant accessed. Defense in depth at the storage layer.

### Metadata DB tenant filtering

`job_runs` lookups always filter `WHERE tenant_id = ?`. Make this impossible to forget:

```python
class ResultStore:
    async def get(self, job_id: str, tenant_id: str) -> ResultRef:
        """tenant_id is required. Returns ResultRef only if (job_id, tenant_id) matches."""

    async def list(self, tenant_id: str, ...) -> list[ResultRef]:
        """tenant_id is required."""
```

Forgetting the filter is the most common multi-tenant bug; making it part of the signature makes it impossible. The `JobContext.tenant_id` flows through naturally.

### Post-policy-filter results

Persisted Parquet is what the principal *was allowed to see during extract*. RLS already trimmed rows; CLS already removed columns. The result is principal-scoped at write time.

Concretely: if tenant_42's extract returned 10K rows under their RLS policy, the persisted Parquet has 10K rows. Replaying the same recipe later for tenant_99 produces a different Parquet (their RLS, their rows). No "result is more permissive than the source" risk — the Parquet inherits the source's policy snapshot at the moment of extract.

## Threat model summary

| Threat | Mitigation | Layer |
|---|---|---|
| Caller forges `tenant_id` | Auth layer validates JWT/mTLS before assertion reaches `nexcraft` | Upstream |
| Caller asks about a source they shouldn't see | `Catalog.get_source(source_id, tenant_id)` returns `SourceNotFoundError` | `nexcraft` |
| Caller crafts SQL to read forbidden rows | Source-side RLS filters at query time | Source |
| Caller crafts SQL to read forbidden columns | Source-side GRANT/masking | Source |
| Provider gives the wrong principal | Connection handle records actual principal; audit log shows mismatch | Audit |
| Pooled connection leaks identity between tenants | Pattern A: `SET LOCAL ROLE` + `RESET ROLE` on release. Pattern C: `set_config(...,true)` for is_local | Provider |
| Result Parquet readable across tenants | Object-storage prefix IAM | `nexcraft-jobs` |
| Metadata query crosses tenants | `tenant_id` in `ResultStore` API signatures | `nexcraft-jobs` |
| Cancelled query keeps running at source | Source-side cancel (`pg_cancel_backend`, `SYSTEM$CANCEL_QUERY`) | Executor |
| Audit log loss | Separate audit sink, independent retention | Operations |

## What we explicitly don't do

- **SQL parsing for security.** No injecting `WHERE tenant_id = ?` into caller SQL. Source-side RLS is the answer.
- **Cross-source policies.** Cross-source compute is recipe-mediated; each extract is single-source and policy-bound at the source. No federation-layer policy engine.
- **Bring-your-own auth.** The auth layer is upstream. `nexcraft.server.flight` and `nexcraft.server.http` provide hook points (JWT validator, mTLS handler) but don't ship an IDP integration. Pluggable.
- **Token rotation.** The `ConnectionProvider` resolves credentials per-query; rotation is a vault concern. We don't cache credentials past the connection lifecycle.

## Operational checklist

Before going to production, an operator confirms:

- [ ] Auth layer validates JWT signatures / mTLS certs and produces a trustworthy `tenant_id` assertion.
- [ ] `Catalog` implementation enforces tenant visibility on `get_source` and `list_sources`.
- [ ] `ConnectionProvider` is one of the three documented patterns, with the appropriate identity-leakage discipline (`SET LOCAL`, `is_local=true`, or per-tenant pool).
- [ ] Source-side RLS / CLS / GRANT policies exist and are tested per principal.
- [ ] Audit log routes to a dedicated audit sink with appropriate retention.
- [ ] `nexcraft-jobs` result bucket has prefix-based IAM enforcing per-tenant access.
- [ ] Cancellation tested end-to-end: client cancel → `nexcraft` cancel → source-side cancel verified by querying the source's process list.
- [ ] If `audit_log_sql: true` is set, the redaction pass has been reviewed for the SQL constructs the application produces.

This list goes in the `how-to/deploy-securely.md` doc in the user-facing site.
