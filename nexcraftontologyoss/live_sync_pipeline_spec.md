# Live Sync Pipeline — Specification

**Status:** Draft 2026-05-16.
**Part of:** Data Knowledge Hierarchy series.
**Depends on:** `mdl_auto_generation_from_source_spec.md`, `mdl_table_concept_annotation_spec.md`, `hierarchy_persistence_and_ingestion_spec.md`, `mdl_bundle_spec.md`, `bundle_publishers_spec.md`, `bundle_consumer_api_spec.md`.
**Stance:** Continuous synchronization is the default. The auto-build pipeline becomes a **one-time bootstrap**; live sync is the steady state.

---

## 1. Scope

This spec defines how the foundry stays continuously synchronized with the data sources it tracks. After the auto-build pipeline (`mdl_auto_generation_from_source_spec.md`) registers a source and produces initial MDL + annotations, a **per-source live-sync worker** takes over: detects schema and metadata changes upstream, propagates them to amundsenrds + sidecars + bundles + Qdrant + publishers, and surfaces drift signals to operations.

The spec covers:
- Per-source change-detection strategy (event-driven where supported, polling fallback).
- State tracking — what we hash, where we store it.
- Cascade rules — which upstream change triggers which downstream work.
- Latency budgets per cascade stage.
- Conflict resolution (source vs human vs service edits).
- Cost management — the LLM re-enrichment is the expensive bit.
- Failure handling, pause/resume, backfill from offline dump.

Out of scope:
- Operational data row replication. We sync **metadata about data**, not data rows. Row replication is the data platform's concern.
- BI tool sync (Looker, Tableau dashboards) — deferred.
- Cross-source equivalence-class refresh — handled by the ontology layer's own batch process, not the live sync pipeline.

---

## 2. Sync model

```
[Source: Postgres / Snowflake / Salesforce / ServiceNow / ...]
        │
        │ change signal (event or polled diff)
        ▼
┌────────────────────────────────────────────────────────────┐
│ Per-source Live Sync Worker (long-running)                  │
│   1. Detect changes (events or scheduled introspection)     │
│   2. Diff against stored state                              │
│   3. Classify change kind                                   │
│   4. Apply to storage transactionally                       │
│   5. Cascade to downstream tasks via reindex_queue          │
└────────────────────────┬────────────────────────────────────┘
                         │
            ┌────────────┼────────────┐
            ▼            ▼            ▼
    annotation     bundle         qdrant
    enrichment    emission        index
            ▼            ▼            ▼
         publishers (per cadence)
```

One worker per (org, source). Workers are stateless; their state lives in Postgres tables (§4). Horizontally scalable by sharding on `source_id`.

### 2.1 Two operating modes

| Mode | Used when | Latency |
|---|---|---|
| **Event-driven** | Source supports change notifications (Postgres event triggers, Salesforce CDC, ServiceNow business rules pushing to webhooks) | Seconds |
| **Polling** | Source supports introspection but not events (Snowflake, MySQL, vendor APIs without push) | Configurable interval (default 60s for fast tier, 5min for slow tier) |

A source may use both: events for what's available + polling fallback for what isn't. The worker reconciles.

### 2.2 Bootstrap → live-sync handoff

When `AutoMDLBuildOrchestrator.build_from_live()` completes for a source:
1. The orchestrator stamps `source_sync_state` (§4.1) with the introspection's `state_hash` and timestamp.
2. The live-sync worker for that source is started (if not already running).
3. The worker begins from the recorded state, watching for further changes.

For sources registered via offline dump (`build_from_dump`), live sync is not available — the source is **frozen** until a live connection is registered.

---

## 3. Change detection per source

Each source kind has its own detection strategy. The worker hides this behind a `ChangeDetector` interface.

### 3.1 `ChangeDetector` Protocol

```python
class ChangeDetector(Protocol):
    source_kind: str

    def supports_events(self) -> bool: ...

    def install(self, *, source_id: str,
                connection: dict) -> InstallResult: ...
    """One-time setup: install event triggers, create event subscriptions, etc.
       Idempotent. Called at first worker start."""

    async def listen(self, *, source_id: str,
                     connection: dict) -> AsyncIterator[SourceChangeEvent]: ...
    """Yields change events as they occur. Used when supports_events=True."""

    async def poll(self, *, source_id: str,
                   connection: dict,
                   since: SourceCheckpoint) -> PollResult: ...
    """Returns changes since the last checkpoint. Used when supports_events=False
       or as a safety net alongside events."""

@dataclass
class SourceChangeEvent:
    kind: Literal["table_created", "table_dropped", "table_renamed",
                  "column_added", "column_dropped", "column_renamed",
                  "column_type_changed", "comment_changed",
                  "fk_added", "fk_dropped",
                  "view_created", "view_changed", "view_dropped",
                  "function_changed", "row_count_changed_significantly"]
    object_kind: Literal["table", "view", "column", "function", "fk", "index"]
    qualified_name: str             # e.g. "public.csod_employee" or "public.csod_employee.first_name"
    new_state: dict | None          # what we observe now (for changes); None for drops
    old_state: dict | None          # what we had stored (for changes); None for creates
    detected_at: datetime
```

### 3.2 Detection strategies by source kind

| Source kind | Event mechanism | Polling mechanism | Notes |
|---|---|---|---|
| **Postgres** | Event triggers (`ddl_command_end`, `sql_drop`) + `LISTEN/NOTIFY` to a foundry-owned channel | `information_schema` snapshot diff every N seconds | Event-trigger install requires DB privileges; gracefully degrade to polling if denied. |
| **Snowflake** | `INFORMATION_SCHEMA` views + `ACCOUNT_USAGE` (with ~45min latency); Streams on `INFORMATION_SCHEMA` not available | Polling every 60s on `INFORMATION_SCHEMA.TABLES`, `COLUMNS`, `VIEWS`, `TABLE_CONSTRAINTS` | Diff-based. Snowflake doesn't push DDL events to subscribers natively. |
| **MySQL** | `binlog` ROW format with `INFORMATION_SCHEMA` polling backup | Polling | `binlog` is data-side; for DDL we polling-only. |
| **Salesforce** | Streaming API + Change Data Capture + Setup Audit Trail | Polling SOQL on `EntityDefinition`, `FieldDefinition` | API-style source; events available but require permissions. |
| **ServiceNow** | Business rules pushing to webhook + `sys_audit` polling | `sys_dictionary` + `sys_db_object` polling | Web hooks require platform configuration. |
| **BigQuery** | Audit Logs via Cloud Logging + Eventarc | `INFORMATION_SCHEMA` polling | Eventarc is GCP-native and requires GCP setup. |
| **dbt project** | Filesystem watch on `manifest.json` (when dbt runs locally) or polling git remote | Polling | Triggered by dbt build, not by data sources directly. |

### 3.3 Implementation locations

```
genieml/dataservices/app/sync/
  __init__.py
  worker.py                       # the per-source live sync worker
  detectors/
    base.py                       # ChangeDetector Protocol
    postgres_detector.py
    snowflake_detector.py
    mysql_detector.py
    salesforce_detector.py
    servicenow_detector.py
    bigquery_detector.py
    dbt_detector.py
  state.py                        # source_sync_state / asset_sync_state DAO
  cascade.py                      # cascade rule engine
  diff.py                         # introspection-result-diff helpers
```

### 3.4 What we don't try to push event-driven

These remain polled (or batch) regardless of source capability:

- **Row count / statistics drift** — too noisy to event-stream; batch-summarized hourly.
- **Sample value distributions** — for enum-detection; sampled daily.
- **Cross-source equivalence updates** — handled by a separate ontology batch.
- **Causal candidate re-extraction from claims** — foundry pipeline; weekly cadence.

---

## 4. State tracking

### 4.1 `source_sync_state`

```sql
CREATE TABLE source_sync_state (
  source_id              text PRIMARY KEY REFERENCES source(source_id) ON DELETE CASCADE,
  sync_mode              text NOT NULL DEFAULT 'auto',  -- 'auto'|'event'|'poll'|'paused'|'frozen'
  detector_kind          text NOT NULL,                  -- matches ChangeDetector.source_kind
  events_supported       boolean NOT NULL DEFAULT false,
  events_installed       boolean NOT NULL DEFAULT false,
  poll_interval_seconds  integer NOT NULL DEFAULT 60,
  last_checkpoint        jsonb,                          -- source-specific (LSN, txn_id, timestamp, ...)
  last_introspection_at  timestamptz,
  last_introspection_hash text,
  worker_started_at      timestamptz,
  worker_heartbeat_at    timestamptz,
  consecutive_failures   integer NOT NULL DEFAULT 0,
  paused_reason          text,
  updated_at             timestamptz NOT NULL DEFAULT now()
);
```

`sync_mode` semantics:
- `auto` — worker picks event or poll based on `events_supported`.
- `event` — event-only (fail if events become unavailable).
- `poll` — polling-only.
- `paused` — operator-paused; resume requires explicit op.
- `frozen` — registered via offline dump; can't sync until live connection is added.

### 4.2 `asset_sync_state`

```sql
CREATE TABLE asset_sync_state (
  source_id              text NOT NULL,
  asset_rk               text PRIMARY KEY,
  asset_kind             text NOT NULL,
  last_seen_at           timestamptz NOT NULL,
  upstream_state_hash    text NOT NULL,        -- hash of (DDL, comments, FKs, indexes) at upstream
  storage_state_hash     text NOT NULL,        -- hash of what we have stored
  hashes_match           boolean GENERATED ALWAYS AS (upstream_state_hash = storage_state_hash) STORED,
  last_sync_event        text,                  -- 'created'|'updated'|'comment_changed'|...
  needs_reenrichment     boolean NOT NULL DEFAULT false,
  flagged_drift          boolean NOT NULL DEFAULT false,
  lifecycle_state        text NOT NULL DEFAULT 'active',  -- 'active'|'removed_upstream'|'paused'
  updated_at             timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_asset_sync_drift ON asset_sync_state (source_id, hashes_match) WHERE NOT hashes_match;
```

`needs_reenrichment` flips true when a change occurred that should re-run annotation enrichment (e.g., description-changed). Cleared when the enrichment task completes.

### 4.3 `sync_event_log`

Append-only log of every detected change, for audit and replay:

```sql
CREATE TABLE sync_event_log (
  event_id        bigserial PRIMARY KEY,
  source_id       text NOT NULL,
  asset_rk        text,
  event_kind      text NOT NULL,
  detected_at     timestamptz NOT NULL DEFAULT now(),
  applied_at      timestamptz,
  applied_outcome text,                   -- 'applied'|'skipped_idempotent'|'skipped_clobber'|'error'
  payload         jsonb NOT NULL,         -- the SourceChangeEvent contents
  error           text
);

CREATE INDEX idx_sync_event_source ON sync_event_log (source_id, detected_at DESC);
CREATE INDEX idx_sync_event_asset ON sync_event_log (asset_rk, detected_at DESC);
```

Retention: 90 days by default; older events are archived to cold storage.

---

## 5. Cascade rules — what triggers what

Each event kind maps to a fixed set of downstream tasks enqueued via the existing `reindex_queue` (`hierarchy_persistence_and_ingestion_spec.md` §4.1) plus a new `annotation_queue`.

| Event | Downstream tasks |
|---|---|
| `table_created` | Insert amundsenrds + sidecars (transactional). Enqueue: `annotation_enrichment(asset_rk)`, `bundle_asset(asset_rk)`, `qdrant_t4(asset_rk)`, `bundle_catalog(catalog_uid)` |
| `table_dropped` | `lifecycle_state='removed_upstream'`; enqueue: `bundle_asset(asset_rk)` (regenerates with deprecated marker), `bundle_catalog(catalog_uid)`, `lineage_orphan_cleanup(asset_rk)` |
| `table_renamed` | Treated as drop+create with provenance link in audit; the new rk gets fresh enrichment, the old rk's annotations are copied if no-clobber permits |
| `column_added` | Update `column_metadata`; enqueue `annotation_enrichment(asset_rk)` (may shift concepts), `bundle_asset(asset_rk)` |
| `column_dropped` | Hard delete from `column_metadata` (amundsenrds cascade); enqueue `bundle_asset`. If column had bindings: `binding_drift_flag` `field_missing_in_asset` |
| `column_renamed` | Update `column_metadata.name`; rk changes; old `binding_field` rows get `card_version_drift` flag |
| `column_type_changed` | Update `column_metadata.col_type`; enqueue `bundle_asset`. No annotation re-run unless semantic_unit shifts. |
| `comment_changed` (table or column) | Update `*_programmatic_description`; enqueue `bundle_asset`. `annotation_enrichment` only if change is significant (> ~100 chars or core terms shift) — throttled per §7. |
| `fk_added` | Insert `lineage_edge`; enqueue `bundle_asset` on both endpoints |
| `fk_dropped` | Mark `lineage_edge.active=false`; enqueue `bundle_asset` on both endpoints |
| `view_created` | Insert as table with `is_view=true`; parse `view_definition.depends_on`; cascade as table_created + per-dep lineage_edge |
| `view_changed` | Update `view_definition`; refresh lineage edges; enqueue `bundle_asset` |
| `view_dropped` | Same as `table_dropped` |
| `function_changed` | Update `function_metadata`; enqueue `bundle_asset` |
| `row_count_changed_significantly` | Update `column_stat` / governance freshness signals; enqueue `governance.json` refresh in bundle. Not necessarily a full re-bundle. |

### 5.1 Annotation re-enrichment throttling

LLM re-enrichment is the expensive cascade. To prevent thrash, the cascade engine throttles per asset:

```python
if last_enrichment_at(asset_rk) > (now() - min_enrichment_interval):
    # Coalesce: mark needs_reenrichment=true; the next scheduled tick picks up.
    mark_pending(asset_rk)
else:
    enqueue_immediate(asset_rk)
```

Default `min_enrichment_interval = 4 hours`. Configurable per source. Schema-structural changes (column added/dropped) bypass the throttle; pure comment changes respect it.

### 5.2 Bundle emission debounce

When many cascade events land on one asset in quick succession, the bundle emitter debounces:

- First event → schedule emission at `now() + 30s`.
- Subsequent events within the window → coalesce; reset the timer if a structural change arrives.
- After the quiet window passes → emit once with all accumulated state.

Default window: 30 seconds. Tuned per ops feedback.

---

## 6. Latency budgets per cascade stage

These are operational SLOs the live-sync system commits to:

| Stage | Target P50 | Target P95 |
|---|---|---|
| Source change → detected by worker | 5s (event) / poll-interval (polling) | 2× target |
| Detected → applied to amundsenrds + sidecars | < 200ms | < 1s |
| Applied → bundle regenerated on disk | < 60s (with debounce) | < 5 min |
| Bundle regenerated → Qdrant re-indexed | < 30s | < 2 min |
| Bundle regenerated → publisher dispatch | per publisher cadence (default 1h) | per cadence × 2 |
| Annotation re-enrichment after structural change | < 5 min | < 30 min (throttle window) |

Per source, configurable; production tier uses tighter values, dev tier looser.

---

## 7. Cost management

### 7.1 Cost drivers

| Cost | Driver | Mitigation |
|---|---|---|
| LLM tokens for re-enrichment | Schema changes triggering annotation | Throttle (§5.1); only structural changes bypass |
| LLM tokens for description gap-fill | Newly-added columns without comments | Native COMMENTs first; LLM only fills gaps |
| Qdrant re-embedding | Narrative-field changes | Payload-only updates when only structural fields shift |
| Publisher API calls | Every bundle change | Publishers run on cadence; coalesce within window |
| Source polling overhead | Polling interval × source count | Tune poll interval per source criticality |

### 7.2 Per-source cost budget

Each source carries a configured monthly cost budget:

```yaml
sources:
  - source_id: csod-servicenow-local
    cost_budget:
      monthly_llm_tokens: 5_000_000
      alert_at_pct: 80
    sync_tier: standard          # standard | high_freq | slow
```

When budget is approached, the cascade engine:
- Increases `min_enrichment_interval` (longer throttle).
- Skips `comment_changed` enrichment triggers entirely.
- Surfaces alert to ops.

Hard cutoff: at 100% of budget, the worker pauses with `paused_reason='cost_budget_exhausted'`. Operator must explicitly resume.

### 7.3 Per-asset hot-spot detection

Some assets churn comments / metadata far more than others. The cost-tracker per-asset flags assets that consume disproportionate budget. Operators can disable per-asset re-enrichment via a flag while keeping the rest of the source live.

---

## 8. Conflict resolution

Live sync introduces a new kind of conflict: the source says one thing, the foundry has stored something different. Resolution depends on **which side authored what**.

### 8.1 Resolution matrix

| Field | Source authored | Human authored | Service authored | LLM authored |
|---|---|---|---|---|
| Column DDL (name, type) | Source wins; we mirror | n/a | n/a | n/a |
| Column COMMENT | Source wins; we mirror | Human edits live in our `*_description` (not `*_programmatic_description`); source updates only `*_programmatic_description`. Both retained. | Same as human | Same |
| Asset `concepts[]` | Not source's concern | Human wins (no-clobber) | Service wins | LLM may overwrite only LLM-prior |
| Asset `key_areas[]` | Not source's concern | Human wins | Service wins | LLM may overwrite only LLM-prior |
| Asset `causal_relations[]` | Not source's concern | Human wins | Service wins | LLM may overwrite only LLM-prior |
| Owners | Source: extracted from source's native ACL when available | Human wins | Service wins | n/a |
| FK | Source declares; we mirror | Human can add inferred FKs that stay parallel | n/a | n/a |

The no-clobber rule from `mdl_table_concept_annotation_spec.md` §5.3 applies uniformly. The sync worker NEVER overwrites annotations whose latest provenance is `human` or `rule_*`.

### 8.2 Description merge

Native source COMMENTs go into `*_programmatic_description` (provenance = `extractor:<source_kind>_information_schema`). Human-authored descriptions go into `*_description` (provenance = `user`). Both are retained simultaneously per amundsenrds' design; bundle emission prefers the user-authored when both exist.

### 8.3 Audit on every conflict resolution

Every time the sync worker skips a field due to no-clobber, it writes an audit row:

```json
{
  "action": "sync_skipped_clobber",
  "asset_rk": "...",
  "field": "concepts",
  "would_have_written": ["customer"],
  "preserved_value": ["customer", "account_holder"],
  "preserved_provenance": "human",
  "preserved_writer": "jane.k@acme.com",
  "preserved_at": "2026-04-12T..."
}
```

Operators can review skipped writes and decide whether to lift the preservation.

---

## 9. Failure handling

### 9.1 Source unavailable

Worker enters back-off loop: 30s, 2m, 5m, 15m, 1h. After 1h of failure, mark `consecutive_failures` and alert ops. Stays in backoff until either source recovers or operator pauses.

State during backoff: `sync_mode` unchanged; `worker_heartbeat_at` continues; `last_introspection_at` does not advance.

### 9.2 Auth failure

Distinct from generic unavailability — auth failures alert immediately and the worker pauses (won't retry until credentials are rotated and the worker is restarted).

### 9.3 Diff oversized

If introspection diff exceeds a configured threshold (e.g., > 50% of tables changed at once), the worker pauses with `paused_reason='diff_too_large'`. This is the safety net for mistaken full-DB swaps (someone restored an old backup, etc.). Operator must explicitly resume.

### 9.4 Cascade task failure

A failed cascade (e.g., annotation enrichment LLM timeout) doesn't roll back the storage write. The cascade task lives in `reindex_queue` and retries per the existing backoff. Worker proceeds to next change.

### 9.5 Worker crash

Heartbeat-based detection. If `worker_heartbeat_at < now() - 5min`, supervisor relaunches. Workers are idempotent; resume from `last_checkpoint`.

---

## 10. Operations

### 10.1 Per-source control

```bash
# Start live sync (after auto-build)
python -m ontology_foundry.cli sync.enable --source-id csod-servicenow-local

# Pause / resume
python -m ontology_foundry.cli sync.pause --source-id csod-servicenow-local --reason "investigating drift"
python -m ontology_foundry.cli sync.resume --source-id csod-servicenow-local

# Force a one-shot reconcile
python -m ontology_foundry.cli sync.reconcile --source-id csod-servicenow-local

# Status
python -m ontology_foundry.cli sync.status --source-id csod-servicenow-local
# Returns: mode, last heartbeat, drift count, cascade queue depth, cost-budget %
```

### 10.2 Pause-all

```bash
# Emergency: pause all sync workers for the org
python -m ontology_foundry.cli sync.pause-all --org-id acme-corp --reason "platform incident"
```

### 10.3 Backfill from dump while live-sync is paused

When a source is paused (e.g., production incident), operators can backfill from an offline dump without disturbing live state:

```bash
# Re-import from dump into a shadow namespace
python -m ontology_foundry.cli source.auto-build \
  --source-id csod-servicenow-local \
  --mode dump \
  --dump-file /backup/snapshot.sql \
  --shadow-namespace csod-servicenow-local-restore

# When confident, promote shadow → main
python -m ontology_foundry.cli source.promote-shadow \
  --source-id csod-servicenow-local \
  --shadow-namespace csod-servicenow-local-restore
```

### 10.4 Per-tenant sync config

`tenants/<org_id>/sync_config.yaml`:

```yaml
live_sync:
  enabled: true                       # global gate
  default_mode: auto
  default_poll_interval_seconds: 60
  per_source:
    csod-servicenow-local:
      mode: auto
      poll_interval_seconds: 30        # higher freq for critical source
      cost_budget:
        monthly_llm_tokens: 5_000_000
    csod-snowflake-prod:
      mode: poll                       # event support TBD
      poll_interval_seconds: 120
    acme-salesforce:
      mode: event                      # CDC fully wired
      poll_interval_seconds: 300       # safety-net polling
```

---

## 11. Observability

Metrics (Prometheus-style):

```
foundry_sync_worker_heartbeat_seconds_since_last{source_id}        # gauge
foundry_sync_events_detected_total{source_id, event_kind}           # counter
foundry_sync_events_applied_total{source_id, event_kind, outcome}   # counter
foundry_sync_cascade_queue_depth{task_kind}                         # gauge
foundry_sync_drift_assets_total{source_id}                          # gauge
foundry_sync_llm_tokens_consumed_total{source_id, purpose}          # counter
foundry_sync_cost_budget_pct{source_id}                             # gauge
foundry_sync_consecutive_failures{source_id}                        # gauge
foundry_sync_cascade_latency_seconds{stage}                         # histogram
```

Ops dashboard surfaces:
- Top-5 drift sources (most assets with `hashes_match=false`).
- Top-5 cost consumers (LLM tokens / hour).
- Cascade-queue depth (alert at sustained > 1000).
- Per-source heartbeat freshness.

---

## 12. Effect on prior specs

### 12.1 `hierarchy_persistence_and_ingestion_spec.md`

§13 reconciler was specced as nightly batch. With live sync:
- Reconciler becomes a **safety net** rather than the primary cadence.
- Runs nightly to catch anything live sync missed (e.g., event triggers that silently stopped firing).
- Discrepancies the reconciler finds raise `flagged_drift` and alert ops.

### 12.2 `mdl_auto_generation_from_source_spec.md`

§9 `DomainWorkflowService.auto_build_from_source` is the **bootstrap** path. After it completes, the worker starts automatically (controlled by sync config). The auto-build orchestrator now emits a `bootstrap_complete` event that the worker consumes to begin from the correct checkpoint.

### 12.3 `mdl_table_concept_annotation_spec.md`

§5.1 trigger list expands: any `comment_changed` event from live sync triggers re-enrichment (throttled per §5.1 here in §7.1).

### 12.4 `bundle_publishers_spec.md`

§3.1 publishers stay on scheduled cadence by default. New option: `cadence: continuous` (publish on every bundle change). High-traffic targets (Purview at scale) may want continuous mode; cost-sensitive ones stay on cron.

### 12.5 `bundle_consumer_api_spec.md` / `mcp_qa_agents_spec.md`

Cache TTLs (§8 of consumer spec; §8.1 of MCP spec) become more aggressive in their **invalidation**. Live sync emits cache-invalidation events keyed by `asset_rk`; cache layers subscribe and evict on receipt.

### 12.6 `evaluation_harness_spec.md`

§5 Drift Resilience eval becomes load-bearing. Pass criteria now include:
- Mean-time-to-detect for synthetic upstream changes < 60s for event sources, < 2× poll interval for poll sources.
- Cascade latency P95 within budgets in §6 here.
- Zero false-positive drift flags in steady state (24h of no upstream changes → no flags raised).

---

## 13. Open items

- **Source schemas with frequent transient changes** (e.g., dynamic columns added by ETL) — currently every column_added triggers enrichment. May need a "transient column" annotation that skips enrichment. Defer.
- **Cross-source rename detection** — if a table is renamed in one source and a downstream replica reflects the rename minutes later, we may double-count the rename. Heuristic dedup based on rename within a recent window. Defer.
- **Snowflake Change-Data-Capture for DDL** — Snowflake recently added more granular DDL event capabilities. Revisit when GA. Currently polling.
- **Cost budget escalation policies** — when a budget is approached, automatically downgrade `min_enrichment_interval` rather than pause. Configurable; defer to operator feedback.
- **Multi-region foundry instances** — when a source spans regions and live sync runs in one region, what's the sync model? Defer to multi-region rollout.

---

## 14. Cross-spec amendments (deferred)

| Spec | Section | Change |
|---|---|---|
| `hierarchy_persistence_and_ingestion_spec.md` | §13 | Reconciler reframed as safety net; live sync is primary. |
| `hierarchy_persistence_and_ingestion_spec.md` | §14 | Add `live-sync-worker-<source_id>` as a per-source worker. |
| `mdl_auto_generation_from_source_spec.md` | §9 | Bootstrap emits `bootstrap_complete` event for live sync handoff. |
| `mdl_table_concept_annotation_spec.md` | §5.1 | Trigger list includes sync `comment_changed` events. |
| `bundle_publishers_spec.md` | §3 | `cadence: continuous` option for publishers. |
| `bundle_consumer_api_spec.md` | §8 | Cache invalidation subscribes to sync events. |
| `mcp_qa_agents_spec.md` | §8 | Same — cache invalidation hooks. |
| `evaluation_harness_spec.md` | §5 | Drift Resilience pass criteria tightened to live-sync SLOs. |

Apply when implementation lands.

---

## 15. Change log

| Date | Change |
|---|---|
| 2026-05-16 | Initial draft. |
