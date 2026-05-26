# 12 — BI Tool Integration

How Tableau, Power BI, DBeaver, and other JDBC/ODBC-shaped BI tools connect to `nexcraft`. This document specifies the Flight SQL introspection surface, the catalog protocol extensions, the driver handler implementations, and the impact on the existing driver design.

## Position

Three principles, in priority order:

1. **Flight SQL is the integration surface.** Tableau and most JDBC-based tools connect via the official `flight-sql-jdbc-driver`. Power BI integrates via a custom Power Query M connector that calls the same Flight SQL endpoint or via the HTTP API. We do not ship a separate JDBC or ODBC driver in v0.1.
2. **Introspection is the driver's job, not the worker's.** BI tools issue metadata queries (`GetTables`, `GetColumns`, `GetSchemas`) before they issue data queries. The driver answers them directly via the catalog and an L1 schema cache. Workers see only data queries.
3. **Schema discovery is tenant-scoped.** A BI tool only sees the tables a tenant is allowed to see. Catalog enforcement is the same boundary as for source visibility — there is no separate metadata-visibility model.

## What BI tools need

When a user clicks "Connect" in Tableau or Power BI and points at a `nexcraft` endpoint, the tool issues a stream of metadata queries before any user query runs:

1. **What catalogs (databases) exist?** Maps to Flight SQL `GetCatalogs`. In `nexcraft` the catalog list is the set of sources the tenant can see.
2. **What schemas exist?** Maps to `GetSchemas`. For warehouse sources this is the source's schema list; for lakehouse sources it's the catalog namespace list.
3. **What tables exist?** Maps to `GetTables`. The tenant-scoped table list per source.
4. **What columns does this table have?** Maps to `GetTables(includeSchema=true)` or implicit in result schema. Returns column names, types, nullability.
5. **What types are supported?** Maps to `GetXdbcTypeInfo`. Static mapping per executor kind.
6. **What primary keys / foreign keys exist?** Maps to `GetPrimaryKeys`, `GetCrossReference`. Optional; tools use it to suggest joins. Implementations may return empty.

All six are RPC actions defined by the Flight SQL spec. The pyarrow `FlightServerBase.flight_sql.FlightSqlServerBase` exposes them as overridable methods. Driver implements them.

After introspection, the tool issues real queries via `CommandStatementQuery` — the existing query path that this design has already specified.

## The split

```
                          BI tool (JDBC/Flight SQL)
                                 │
                ┌────────────────┴────────────────┐
                │                                 │
                ▼                                 ▼
        Introspection actions             CommandStatementQuery
        (GetTables, GetColumns…)          (SELECT … FROM …)
                │                                 │
                ▼                                 ▼
        Driver introspection           Driver query handler
        handlers                       (existing path:
                │                       cache → admission →
                ▼                       routing → worker)
        Catalog API +
        L1 schema cache
```

The architectural point: **introspection and data queries are two paths sharing the same driver and the same workers**, but the introspection path never goes through the cache-result / admission / routing / worker-dispatch sequence. It's a separate handler track.

See [`01-architecture.md`](01-architecture.md) for the platform diagram and request-flow walkthrough.

## Catalog protocol extensions

The current `Catalog` protocol (in `02-protocols.md`) handles "given a source_id, return the source descriptor." For BI tools it needs to answer schema discovery questions. Three new methods.

```python
@dataclass(frozen=True)
class TableRef:
    """A table identifier within a source."""
    catalog: str | None         # rarely populated; most sources don't have multi-catalog
    schema: str                 # e.g. "public", "analytics"; "" for sources without schemas
    name: str                   # the table name
    table_type: str = "TABLE"   # "TABLE" | "VIEW" | "EXTERNAL" | etc.

@dataclass(frozen=True)
class ColumnInfo:
    """A column within a table."""
    name: str
    arrow_type: pa.DataType
    nullable: bool = True
    ordinal: int = 0
    default: str | None = None
    comment: str | None = None

@dataclass(frozen=True)
class TableSchema:
    """A table's full schema."""
    ref: TableRef
    columns: list[ColumnInfo]
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list["ForeignKeyInfo"] = field(default_factory=list)
    comment: str | None = None

@runtime_checkable
class Catalog(Protocol):
    # ... existing methods (get_source, list_sources) ...

    async def list_schemas(
        self,
        source_id: str,
        tenant_id: str,
    ) -> list[str]:
        """Returns schemas the tenant can see in this source.

        For sources without schemas (e.g. some lakehouses), returns [""].
        Raises SourceNotFoundError if the tenant can't reach source_id.
        """

    async def list_tables(
        self,
        source_id: str,
        tenant_id: str,
        schema: str | None = None,
    ) -> list[TableRef]:
        """Returns tables the tenant can see in this source.

        If schema is None, returns tables from all schemas the tenant can see.
        Raises SourceNotFoundError if the tenant can't reach source_id.
        """

    async def describe_table(
        self,
        source_id: str,
        tenant_id: str,
        schema: str,
        table: str,
    ) -> TableSchema:
        """Returns the schema of a specific table.

        Raises TableNotFoundError if the table doesn't exist OR if the tenant
        cannot see it. As with sources, no information leak about table existence
        when access is denied.
        """
```

### Tenant scoping

All three methods take `tenant_id`. The catalog enforces visibility. A tenant who can't see source X gets `SourceNotFoundError`; a tenant who can see source X but can't see table T gets `TableNotFoundError`. The error shape is the same as for nonexistent — no information leak.

For most sources, table-level filtering inside a visible source is delegated to the source itself (Postgres GRANTs, Snowflake role-based access). The catalog can either:

- **Trust the source** — return everything the catalog knows about; if a tenant queries a table they can't actually read, the source returns a permission error. Simple, defers to source-side enforcement.
- **Pre-filter** — the catalog maintains its own allowlist per tenant. Useful when you want introspection to match runtime access exactly.

Reference catalog implementations: `YAMLCatalog` does pre-filtering; production catalogs (which integrate with the org's IAM) typically trust the source.

### Where the schema data comes from

Two strategies, depending on the catalog implementation:

**Static catalog** (`YAMLCatalog`, `StaticCatalog`):
- Schema data is declared up front in configuration.
- Cheap to serve; never queries the source.
- Goes stale if the source schema changes.

**Live catalog** (`SourceIntrospectingCatalog`):
- On a miss, the catalog dispatches an introspection query to a worker (`SourceExecutor.describe_source()` — see below).
- Results are cached in L1.
- TTL of hours; manual refresh API for explicit invalidation.

Production deployments use a combination: source descriptors and visibility policies come from a static or service-backed source, while table-level schema discovery is live.

## SourceExecutor extension (optional)

For catalogs that want to introspect live sources, executors gain an **optional** method:

```python
class SourceExecutor(Protocol):
    # ... existing methods (kind, describe, execute) ...

    async def describe_source(
        self,
        ctx: QueryContext,
        conn: ConnectionHandle,
        scope: "DescribeScope" = "tables",
    ) -> "SourceDescription":
        """Returns information about the source's schemas, tables, and columns.

        Optional method — implementations may raise NotImplementedError if they
        don't support introspection. The catalog handles NotImplementedError by
        falling back to whatever static data it has, or returning an empty list.

        `scope` controls how much to return:
          - "schemas" → list of schemas only (fast)
          - "tables"  → schemas + tables, no column details (default)
          - "full"    → schemas + tables + columns (slow on large catalogs)
        """
```

Per-executor implementations:

| Executor | Introspection mechanism |
|---|---|
| Postgres | `SELECT … FROM information_schema.schemata / tables / columns / key_column_usage` |
| Snowflake | `SHOW SCHEMAS / SHOW TABLES / DESC TABLE` or `INFORMATION_SCHEMA` views |
| BigQuery | `INFORMATION_SCHEMA.SCHEMATA / TABLES / COLUMNS` |
| Iceberg | `pyiceberg.catalog.Catalog.list_namespaces() / list_tables() / load_table()` — no SQL needed |
| Delta | Object-store listing + per-table `_delta_log/` parse |

The optional method keeps third-party executors easy to write: implementers who don't care about BI integration skip it. Catalogs that need introspection from sources without `describe_source()` fall back to static configuration.

## L1 schema cache

The schema cache lives in the same Redis as the result cache, with a separate key namespace:

```
Key:      schema:{source_id}:{tenant_id}:{schema}:{table}
Value:    serialized TableSchema (Arrow IPC or msgpack)
TTL:      configurable per source, default 1 hour
```

Per-source defaults:

```yaml
sources:
  prod_pg:
    cache:
      schema_ttl: 3600           # 1 hour for table schemas
      schemas_list_ttl: 1800     # 30 min for list_schemas / list_tables results
```

### Invalidation

Three triggers:

1. **TTL expiry** — default. Schema changes are rare; 1 hour is fine.
2. **Schema mismatch on execute** — if a query's result schema doesn't match the cached `describe_table` schema, invalidate the cached entry. The next introspection refresh from the source. (Already specified in `11-caching.md` for the describe cache.)
3. **Manual purge via admin API** — `POST /admin/schema-cache/purge?source_id=prod_pg` for operators when an out-of-band schema change requires forcing a refresh.

### Cache key tenant-scoping

The schema cache key includes `tenant_id` because different tenants can see different tables in the same source. Even though the schema *content* doesn't depend on tenant, the *list of tables the tenant sees* does.

A future optimization could split the cache:
- `schema:source:_:schema:table` — tenant-independent column data (cached once per table).
- `tables_for_tenant:source:tenant_id` — per-tenant table visibility (cheap list of refs).

For v0.1, keep it simple: per-tenant cache entries. The duplication is small (table schemas are tens of KB), and the simpler key model avoids the bug class where the two-tier cache gets out of sync.

## Driver introspection handlers

The pyarrow Flight SQL server base class exposes one method per introspection action. The driver overrides each.

```python
import pyarrow.flight as fl
import pyarrow.flight.sql as fl_sql

class NexcraftFlightSqlServer(fl_sql.FlightSqlServerBase):

    def __init__(self, driver: "Driver"):
        super().__init__()
        self._driver = driver

    # CATALOGS ─────────────────────────────────────────────────────
    def get_flight_info_catalogs(self, context, descriptor):
        tenant = self._driver.auth.tenant_from_context(context)
        # In nexcraft, each source is a "catalog" from BI tool perspective
        sources = asyncio.run(self._driver.catalog.list_sources(tenant.tenant_id))
        schema = fl_sql.GetCatalogs.schema  # standard Flight SQL schema
        return self._make_flight_info(descriptor, schema, ticket={
            "action": "list_catalogs",
            "tenant_id": tenant.tenant_id,
        })

    def do_get_catalogs(self, context, ticket):
        # Stream back the result
        ...

    # SCHEMAS ──────────────────────────────────────────────────────
    def get_flight_info_schemas(self, context, descriptor, command):
        tenant = self._driver.auth.tenant_from_context(context)
        source_id = command.catalog  # "catalog" in Flight SQL = source in nexcraft
        schemas = asyncio.run(
            self._driver.catalog.list_schemas(source_id, tenant.tenant_id)
        )
        # Filter by command.db_schema_filter_pattern if set (LIKE pattern)
        ...

    # TABLES ───────────────────────────────────────────────────────
    def get_flight_info_tables(self, context, descriptor, command):
        tenant = self._driver.auth.tenant_from_context(context)
        source_id = command.catalog
        tables = asyncio.run(self._driver.catalog.list_tables(
            source_id, tenant.tenant_id, schema=command.db_schema_filter_pattern,
        ))
        # If command.include_schema is True, also fetch describe_table for each
        # (potentially expensive; rate-limit aggressively)
        ...

    # COLUMNS, KEYS, TYPES — all follow the same pattern
    ...
```

### Important implementation details

**Authentication via Flight context.** The driver's auth middleware validates JWT or mTLS on the gRPC call and attaches the tenant identity to the Flight context. Introspection handlers read it from there. Same auth path as data queries.

**Async bridging.** pyarrow Flight server methods are synchronous; the driver's async catalog calls happen via `asyncio.run_coroutine_threadsafe` to a shared event loop. This is the same bridge the existing `do_action` query path uses.

**Filter patterns.** Flight SQL passes LIKE patterns (`%`, `_`) for filtering schemas/tables. The driver applies these *after* fetching from the catalog — pushing patterns to source-side introspection queries is a nice optimization but not required for correctness.

**Result encoding.** Each introspection action has a canonical Arrow schema defined by the Flight SQL spec. The driver returns `RecordBatch`es matching exactly — column order, type, nullability all matter, because the BI tool's JDBC driver parses them positionally. Mistakes here produce silent malformations in the tool's UI.

**Per-handler observability.** Introspection actions emit their own OTel spans (`nexcraft.flight.get_tables`, `nexcraft.flight.get_columns`) and their own metrics (`nexcraft_introspection_requests_total{action, outcome}`). Separate from data-query metrics — different traffic patterns, different alerting needs.

### Type mapping

Each executor's source-side types must map to Arrow types in a way that produces sensible BI tool behavior. The mapping happens in two places:

1. **Result schemas** — already specified per executor; BI tools read directly.
2. **`GetColumns` responses** — must include both Arrow types AND the SQL type names BI tools expect (`xdbc_type_name`, `xdbc_data_type`).

A reference mapping table per source kind lives in the executor docs. Example for Postgres:

| Postgres type | Arrow type | XDBC name | XDBC data type |
|---|---|---|---|
| `int4` | `int32` | `INTEGER` | `INTEGER` (4) |
| `int8` | `int64` | `BIGINT` | `BIGINT` (-5) |
| `numeric(p,s)` | `decimal128(p,s)` | `NUMERIC` | `NUMERIC` (2) |
| `text` | `utf8` | `VARCHAR` | `VARCHAR` (12) |
| `timestamptz` | `timestamp[us, UTC]` | `TIMESTAMP_WITH_TIMEZONE` | `TIMESTAMP_WITH_TIMEZONE` (93) |
| `jsonb` | `utf8` | `VARCHAR` | `VARCHAR` (12) |

`jsonb` as varchar is the pragmatic choice — BI tools don't have a JSON column type, so we surface JSON as a string and document it. The same applies to `uuid`, `inet`, `cidr`, and other Postgres-specific types.

## How the driver design is affected

The driver/worker architecture from `10-driver-worker.md` is preserved. The additions are bounded and additive — no breaking changes to existing components.

### Components added

| Component | Where | What |
|---|---|---|
| Flight SQL introspection handlers | Driver | Six new method overrides on the Flight SQL server. Each reads tenant from auth context, calls catalog, returns Arrow result. |
| `Catalog.list_schemas / list_tables / describe_table` | Catalog protocol | Three new methods on the existing protocol. |
| `SourceExecutor.describe_source` (optional) | Executor protocol | One optional method. Existing executors don't need to implement it; catalogs handle `NotImplementedError`. |
| L1 schema cache | Driver | New Redis key namespace; reuses existing Redis client. Same cache backend protocol. |
| Reference `SourceIntrospectingCatalog` | Catalog impls | New impl that uses `describe_source()` to populate L1 lazily. |
| Per-executor introspection mapping | Executors | Each executor gains a `describe_source()` impl (optional) and a type mapping table. |

### What does NOT change

- The `QueryContext`, `ConnectionProvider`, `ConnectionHandle`, and error hierarchy from `02-protocols.md`.
- The streaming primitive (`CancellableArrowStream`).
- The L0 result cache shape and modes.
- The admission control + routing path for data queries.
- The worker's internal Flight protocol (`nexcraft.WorkerExecute`).
- The HTTP server's request shape.
- The security model.

The data-query path is untouched. BI tool query traffic flows through the same admission → cache → routing → worker → executor path as agent traffic. The only difference is that the driver now also serves introspection actions in front of that path.

### Driver process model

The driver's gRPC Flight server now handles two RPC types:

```
                  ┌──────────────────────────────────┐
                  │  Driver Flight SQL gRPC server   │
                  └────────────────┬─────────────────┘
                                   │
            ┌──────────────────────┴──────────────────────┐
            │                                             │
            ▼                                             ▼
   Introspection actions                       Data query actions
   - get_flight_info_catalogs                  - get_flight_info_statement
   - get_flight_info_schemas                   - do_get(ticket)
   - get_flight_info_tables                    - do_action("nexcraft.Execute")
   - get_flight_info_columns                   - do_action("nexcraft.Cancel")
   - get_flight_info_primary_keys
   - get_flight_info_cross_reference
   - get_flight_info_xdbc_type_info
            │                                             │
            ▼                                             ▼
   Direct to Catalog                           Existing data-query handler
   - tenant-scoped                             - cache check
   - L1 cache lookup                           - admission
   - on miss: worker introspection             - routing
                                               - worker dispatch
                                               - stream proxy + L0 population
```

Both paths share auth middleware, observability infrastructure, audit logging, and Redis. They diverge only in the handler.

### Worker process model

Workers gain one optional capability:

- If their executors implement `describe_source()`, the driver can route introspection queries to them.
- Introspection queries are dispatched via the same `WorkerExecute` internal Flight protocol with a flag indicating "this is introspection, not a regular query."
- Workers process introspection queries at lower priority than data queries (they're rate-limited; a flood of introspection from a confused BI tool can't starve real work).

Workers that don't implement `describe_source()` are unaffected. Mixed-capability worker pools are supported — the driver routes introspection only to workers that can serve it.

## Power BI considerations

Power BI doesn't ship a Flight SQL connector. Three integration paths:

### Path 1 — Custom Power Query M connector (recommended)

Ship a `.mez` file. Customer installs in their Power BI Desktop. The connector calls `nexcraft`'s HTTP API (not Flight SQL) — Power Query M is JSON-over-HTTP friendly.

**What we ship:**
- `nexcraft.mez` — Power Query M connector code in `Nexcraft.Connector.pq`.
- Documentation for installation, auth setup, query patterns.
- Code-signing process documentation (Microsoft cert for public distribution; self-signing for internal).

**Capabilities:**
- Connect with username/password → driver issues JWT.
- Browse sources, schemas, tables in the navigator pane.
- Import mode works fully.
- DirectQuery mode requires query folding into source-dialect SQL. The connector forwards Power Query M `M` expressions; the driver doesn't translate them. So DirectQuery support is limited — operators can preview but not push aggregations down. This is a future enhancement.

### Path 2 — Power BI generic ODBC connector via Flight SQL ODBC driver

If the customer has the Apache Arrow Flight SQL ODBC driver installed, Power BI's generic ODBC connector can use it. Power BI sees `nexcraft` as an ODBC source. Works, but ODBC driver maturity is the limiting factor as of 2026.

We don't ship the Flight SQL ODBC driver; the Apache Arrow project does. We document its installation and known limitations.

### Path 3 — Power BI Web data source against HTTP API

Power BI has a generic Web connector that fetches CSV / JSON. The driver's HTTP API can return small results in JSON. Acceptable for static reports refreshed on schedule, not for interactive analysis.

### Recommendation

**Path 1 for v0.1.** Ship a usable Power Query M connector as part of the platform release. Document its limitations honestly. Customers with serious Power BI needs invest in DirectQuery folding as a separate workstream — likely a v0.3 conversation.

## Tableau considerations

Tableau works today via the official `flight-sql-jdbc-driver`. Three deployment shapes:

### Shape 1 — Generic JDBC connector (works out of the box)

User installs `flight-sql-jdbc-driver.jar` into `~/Library/Tableau/Drivers/` (Mac) or equivalent. Tableau "Other Databases (JDBC)" connector with URL `jdbc:arrow-flight-sql://nexcraft.example.com:50051`. Auth via username/password → JWT.

Works for Tableau Desktop and Tableau Server.

### Shape 2 — Tableau Connector SDK (TCS) wrapper

A small `.taco` package wrapping the JDBC driver with:
- Custom dialog ("Connect to nexcraft" with branded logo, helpful field labels).
- Auth flow specific to nexcraft (e.g., OAuth handoff to the customer's IDP).
- Pre-configured connection-string parameters.

The TCS package is ~50 lines of XML + a manifest. The actual driver is still the Flight SQL JDBC. We ship the TCS as a sample.

### Shape 3 — Tableau Cloud

Tableau Cloud (the SaaS) doesn't allow custom JDBC drivers. Customers using Tableau Cloud need either:
- A `nexcraft` deployment reachable from Tableau Cloud (with appropriate auth), and
- Use of Tableau's published built-in connectors. As of 2026, the closest fit is Snowflake (if we ever add Postgres wire, this becomes much easier).

We document this limitation. Path: customers with Tableau Cloud + nexcraft tier their data through `nexcraft-jobs` recipes into Snowflake, and Tableau Cloud reads from Snowflake. Indirect but works.

### Recommendation

**Shape 1 + Shape 2 for v0.1.** Document JDBC connection. Ship a sample TCS package. Tableau Cloud is acknowledged as a limitation; document the workaround.

## What v0.1 ships

| Deliverable | Where |
|---|---|
| Six Flight SQL introspection handlers | Driver code |
| `Catalog` protocol extensions (`list_schemas`, `list_tables`, `describe_table`) | Core protocol |
| `SourceExecutor.describe_source` (optional) | Core protocol |
| `SourceIntrospectingCatalog` reference impl | `nexcraft.catalog` |
| Per-executor `describe_source` impls (Postgres, Snowflake, Iceberg) | Executors |
| Per-executor type mapping tables (XDBC types) | Executor docs |
| L1 schema cache | Driver Redis namespace |
| Sample Tableau TCS package | `examples/tableau/` |
| Sample Power BI Power Query M connector (`.mez` source) | `examples/powerbi/` |
| `how-to/connect-tableau.md` | User docs |
| `how-to/connect-powerbi.md` | User docs |

## What v0.1 does NOT ship

- A first-party `nexcraft` JDBC driver (we use Flight SQL JDBC).
- A first-party `nexcraft` ODBC driver (Flight SQL ODBC exists upstream).
- Postgres wire protocol (potential v0.3 if pull justifies).
- Power BI DirectQuery with full pushdown.
- Tableau Cloud direct integration (workaround documented).
- Schema change notification (BI tool sees stale data until TTL expiry; this is acceptable).

## Operational checklist

Before BI tools can connect in production:

- [ ] Driver Flight SQL server has all six introspection handlers wired up.
- [ ] Catalog implementation supports `list_schemas`, `list_tables`, `describe_table`.
- [ ] L1 schema cache TTLs configured per source.
- [ ] Per-source type mapping tables verified against actual source schemas.
- [ ] At least one BI tool tested end-to-end: connect, browse navigator, query a table, refresh schema.
- [ ] Auth flow tested with the BI tool's credential storage (Tableau saved connections, Power BI gateway credentials).
- [ ] Introspection metrics and audit logging verified (separate from data-query metrics).
- [ ] Rate limits on introspection set (a misbehaving BI tool shouldn't be able to DoS the catalog).
- [ ] `examples/tableau/` and `examples/powerbi/` packages built and tested.

This list goes in the `how-to/deploy-bi-integration.md` doc on the user-facing site.
