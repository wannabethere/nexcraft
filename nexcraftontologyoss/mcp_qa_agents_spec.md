# MCP Q&A Agents — Specification

**Status:** Draft 2026-05-16.
**Part of:** Data Knowledge Hierarchy series.
**Depends on:** `bundle_consumer_api_spec.md` (BundleStore + OntologyContextLoader), `semantic_layer_card_spec.md`, `mdl_bundle_spec.md`, `mdl_table_concept_annotation_spec.md`, `mdl_auto_generation_from_source_spec.md`.
**Purpose:** Expose the ontology graph and bundle store as an **MCP server** so users can ask data-model and data-assistance questions from any MCP-compatible client (Claude Desktop, Cursor, custom apps). Also exposes a server-side **Q&A agent** that wraps multi-tool orchestration into a single business-question answering call.

---

## 1. Scope

This spec defines:

1. The **MCP server** — what tools and resources the foundry exposes via the Model Context Protocol.
2. The **per-tool contracts** — input schemas, output schemas, error handling.
3. The **Q&A agent** — a server-side agent that combines multiple tool calls to answer business questions, exposed as one `ask` tool plus a streamed-response variant.
4. **Auth + tenant scoping** — every MCP session is bound to one (org, user) tuple with role-based scoping.
5. **Performance + cost** — tool execution budgets, caching, rate limits.

Out of scope:
- Building / authoring cards (operator-facing UI is separate).
- Publishing to external catalogs (`bundle_publishers_spec.md`).
- The auto-build pipeline (prior spec).

---

## 2. Architecture

```
[MCP Client: Claude Desktop / Cursor / Custom app]
        │
        │  stdio or HTTP+SSE transport
        ▼
┌──────────────────────────────────────────────────────────┐
│  Foundry MCP Server                                       │
│  ─────────────────────────────                            │
│  • Tools (primitive + agentic)                            │
│  • Resources (browsable URIs)                             │
│  • Prompts (reusable templates)                           │
│  • Auth middleware                                        │
│  • Tenant scoping                                         │
└─────────────┬────────────────────────────────────────────┘
              │
   ┌──────────┴──────────────────────────────────────┐
   ▼                                                 ▼
BundleStore                                OntologyContextLoader
(per bundle_consumer_api_spec)            (per bundle_consumer_api_spec §4)
   │                                                 │
   ▼                                                 ▼
Postgres + Qdrant + filesystem            CardStore + BundleStore
```

The MCP server is a **thin protocol adapter** over the consumer API. The agentic intelligence is either client-side (the user's LLM driving primitive tools) or server-side (the `ask` tool's agent).

### 2.1 Transport modes

| Mode | Use case |
|---|---|
| `stdio` | Local development; embedded in Claude Desktop config |
| HTTP + SSE | Hosted server; multi-user; standard MCP over HTTP |
| WebSocket (future) | Streaming agentic responses with bidirectional events |

Both stdio and HTTP modes are v1. The server uses the `mcp` Python SDK (anthropic-ai/mcp-server reference implementation).

### 2.2 Server identity

```json
{
  "name": "ontology-foundry",
  "version": "0.1.0",
  "description": "Data-knowledge ontology Q&A and metadata browsing for an organization."
}
```

---

## 3. Tools — primitive (client-driven LLM orchestrates these)

These are the building blocks. A client-side LLM (Claude in Claude Desktop, etc.) picks among them to answer a user's question.

### 3.1 `search_tables`

```json
{
  "name": "search_tables",
  "description": "Semantic search over data tables, views, and API endpoints. Returns ranked summaries.",
  "inputSchema": {
    "type": "object",
    "required": ["query"],
    "properties": {
      "query":             { "type": "string", "description": "Natural-language query" },
      "source_id":         { "type": "string", "description": "Optional: restrict to one source" },
      "asset_kind":        { "type": "string", "enum": ["table","view","materialized_view","api_endpoint","function","metric"] },
      "lifecycle_stage":   { "type": "string", "enum": ["production","development","deprecated","archived"] },
      "concepts":          { "type": "array", "items": { "type": "string" } },
      "key_areas":         { "type": "array", "items": { "type": "string" } },
      "k":                 { "type": "integer", "default": 10, "maximum": 50 }
    }
  }
}
```

Output: list of `{ rk, name, asset_kind, summary, score, payload }`. Backed by `BundleStore.search_assets`.

### 3.2 `describe_table`

```json
{
  "name": "describe_table",
  "description": "Return the MDL + key context for one asset by its rk.",
  "inputSchema": {
    "type": "object",
    "required": ["rk"],
    "properties": {
      "rk":      { "type": "string" },
      "include": { "type": "array", "items": { "type": "string",
                   "enum": ["mdl","context","semantic_bindings","governance","causal","metrics"] } }
    }
  }
}
```

Output: subset of bundle concerns the caller requested. Backed by `BundleStore.get_bundle(include_concerns=...)`.

### 3.3 `search_concepts`

```json
{
  "name": "search_concepts",
  "description": "Semantic search over semantic-layer concept cards (object_type, interface, causal_node, etc.).",
  "inputSchema": {
    "type": "object",
    "required": ["query"],
    "properties": {
      "query":      { "type": "string" },
      "kind":       { "type": "array", "items": { "type": "string",
                      "enum": ["object_type","interface","causal_node","derived_state","action","metric","event"] } },
      "markings":   { "type": "array", "items": { "type": "string" } },
      "k":          { "type": "integer", "default": 10 }
    }
  }
}
```

Output: list of card hits with frontmatter + first-paragraph excerpts. Backed by `BundleStore.search_cards`.

### 3.4 `describe_concept`

```json
{
  "name": "describe_concept",
  "description": "Return the full card body for a concept by id+kind.",
  "inputSchema": {
    "type": "object",
    "required": ["card_id","kind"],
    "properties": {
      "card_id": { "type": "string" },
      "kind":    { "type": "string" }
    }
  }
}
```

Output: `{ id, kind, version, frontmatter, body, refs_resolved: [...] }`.

### 3.5 `find_metrics`

```json
{
  "name": "find_metrics",
  "description": "Find metrics relevant to a concept, key area, or causal node.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "concept_id":      { "type": "string" },
      "key_area":        { "type": "string" },
      "causal_node_id":  { "type": "string" },
      "k":               { "type": "integer", "default": 10 }
    }
  }
}
```

Output: ranked metrics with their MDL excerpts and `primary_asset_rk`.

### 3.6 `trace_lineage`

```json
{
  "name": "trace_lineage",
  "description": "Show upstream / downstream lineage for an asset.",
  "inputSchema": {
    "type": "object",
    "required": ["rk"],
    "properties": {
      "rk":         { "type": "string" },
      "direction":  { "type": "string", "enum": ["upstream","downstream","both"], "default": "both" },
      "edge_kinds": { "type": "array", "items": { "type": "string" } },
      "max_hops":   { "type": "integer", "default": 2 }
    }
  }
}
```

Output: lineage graph with edges + node summaries. Backed by `BundleStore.lineage`.

### 3.7 `find_owners`

```json
{
  "name": "find_owners",
  "description": "Return ownership for an asset or a set of assets.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "rk":      { "type": "string" },
      "concept": { "type": "string", "description": "Find owners for all assets bound to this object_type" }
    }
  }
}
```

Output: list of `{ rk, owner, role, since }`.

### 3.8 `find_related_assets`

```json
{
  "name": "find_related_assets",
  "description": "Assets that share concepts, key areas, equivalence classes, or causal participation with a given anchor.",
  "inputSchema": {
    "type": "object",
    "required": ["rk"],
    "properties": {
      "rk":  { "type": "string" },
      "via": { "type": "array", "items": { "type": "string",
              "enum": ["shared_concept","shared_key_area","equivalence_class","lineage","causal_participation"] } },
      "k":   { "type": "integer", "default": 10 }
    }
  }
}
```

Output: ranked list of related assets with the relation reason.

### 3.9 Tool result schema (common envelope)

Every tool returns:

```json
{
  "ok": true,
  "data": { ... tool-specific ... },
  "warnings": [ "..." ],
  "tenant_scope": { "org_id": "...", "user_id": "..." },
  "wall_time_ms": 42
}
```

Errors return `{ "ok": false, "error": { "code": "...", "message": "..." } }` with non-200 HTTP when over HTTP transport.

---

## 4. Tools — agentic

### 4.1 `ask`

The high-level Q&A tool. Server-side orchestrates context loading + LLM synthesis. Used when the client wants a single-call answer rather than driving primitive tools.

```json
{
  "name": "ask",
  "description": "Ask a business question about data, governance, or causal relationships. Server-side agent assembles context and synthesizes an answer.",
  "inputSchema": {
    "type": "object",
    "required": ["question"],
    "properties": {
      "question":      { "type": "string" },
      "intent_hint":   { "type": "string",
                         "enum": ["compliance_rec","dashboard_rec","causal_reasoning",
                                  "schema_lookup","entity_resolution","governance_lookup"],
                         "description": "Optional; server infers if absent." },
      "anchors_hint":  { "type": "array", "items": { "type": "string" },
                         "description": "Optional: card ids or asset rks to pin context on" },
      "scope":         { "type": "object",
                         "properties": {
                           "concepts":   { "type": "array", "items": { "type": "string" } },
                           "key_areas":  { "type": "array", "items": { "type": "string" } },
                           "source_ids": { "type": "array", "items": { "type": "string" } }
                         }
                       },
      "max_tokens":    { "type": "integer", "default": 1500 },
      "stream":        { "type": "boolean", "default": false }
    }
  }
}
```

Output (non-streaming):

```json
{
  "answer":         "...",
  "citations":      [ { "rk": "...", "kind": "table", "claim_id": "..." }, ... ],
  "context_used":   { "cards_full": 4, "cards_summary": 6, "cards_manifest": 11,
                      "bundles_loaded": 3, "estimated_tokens": 9420 },
  "intent_used":    "compliance_rec",
  "warnings":       [ ... ]
}
```

Streaming mode: SSE chunks with `{ "type": "text", "delta": "..." }` followed by a terminal `{ "type": "done", "citations": [...], ... }`.

### 4.2 `ask` server-side flow

```
1. Resolve anchors: 
   - If anchors_hint provided, validate.
   - Else: BundleStore.search_cards(question, k=5) + BundleStore.search_assets(question, k=10)
   - Pick top combined.
2. Infer intent if not provided:
   - Lightweight LLM call OR rule: question keywords + anchor card kinds.
3. Apply ContextPolicy for the intent.
4. OntologyContextLoader.load(anchors, intent, policy)
5. Build prompt from AssembledContext.render_prompt() + the question.
6. LLM call: synthesize answer with citation requirements (must cite by rk).
7. Validate citations resolve to assets/cards in the context.
8. Return answer + citations + context_used summary.
```

LLM call count for `ask` (clean run): 1–2 (intent inference if not provided + answer synthesis).

### 4.3 `compare_sources`

```json
{
  "name": "compare_sources",
  "description": "Given a concept, compare how it's modeled across multiple sources (equivalence class detection).",
  "inputSchema": {
    "type": "object",
    "required": ["concept_id"],
    "properties": {
      "concept_id":  { "type": "string" },
      "source_ids":  { "type": "array", "items": { "type": "string" } }
    }
  }
}
```

Output: side-by-side comparison of assets binding to the concept across sources, with field-level alignment from `semantic_bindings` and equivalence class memberships.

### 4.4 `recommend_metrics`

```json
{
  "name": "recommend_metrics",
  "description": "Given a business question, recommend metrics to track (uses the foundry's recommendation pipeline).",
  "inputSchema": {
    "type": "object",
    "required": ["question"],
    "properties": {
      "question":     { "type": "string" },
      "compliance_regime": { "type": "string" },
      "domain":       { "type": "string" },
      "k":            { "type": "integer", "default": 5 }
    }
  }
}
```

Output: recommended metrics with rationale + dashboard layout hint.

---

## 5. Resources — URI-addressable content

MCP resources are passive content the client can `read` by URI. Useful for the client to pull whole bundle parts without invoking tools.

| URI pattern | Returns |
|---|---|
| `ofdy://tenant/{org_id}/asset/{rk}/mdl` | The asset's `mdl.json` |
| `ofdy://tenant/{org_id}/asset/{rk}/bundle` | Full bundle manifest |
| `ofdy://tenant/{org_id}/asset/{rk}/{concern}` | Specific bundle concern (`context`, `governance`, `causal`, ...) |
| `ofdy://tenant/{org_id}/card/{kind}/{id}` | Card body + frontmatter |
| `ofdy://tenant/{org_id}/catalog/{catalog_uid}/index` | Catalog's assets index |
| `ofdy://tenant/{org_id}/source/{source_id}/summary` | Source overview |

URI scheme `ofdy://` for "ontology foundry". Resources can be listed with `resources/list` for client-side discovery; large tenants may paginate.

---

## 6. Prompts — reusable templates

MCP prompts are pre-built prompt templates the user invokes by name. They take parameters and return a fully-formed user message.

### 6.1 `compliance_dashboard_proposal`

```yaml
name: compliance_dashboard_proposal
description: Propose a compliance dashboard for a given regulatory regime + domain.
arguments:
  - name: regime
    description: e.g. HIPAA, SOX, GDPR
    required: true
  - name: domain
    description: e.g. Clinical, Finance, Sales
    required: false
template: |
  Propose a {{regime}} compliance dashboard for the {{domain or 'organization'}}.
  Identify the key metrics, the assets they read from, the owners accountable
  for each, and the causal mechanisms linking metric movement to compliance risk.
  Cite each metric by rk and ground each claim in the foundry's causal layer.
```

### 6.2 `causal_explainer`

```yaml
name: causal_explainer
description: Explain why a metric is moving by tracing its causal upstream.
arguments:
  - name: metric_rk
    required: true
  - name: observation
    description: e.g. "down 15% MoM"
    required: false
```

### 6.3 `data_asset_summary`

```yaml
name: data_asset_summary
description: Generate a one-page summary for an asset including its concept, governance, and lineage.
arguments:
  - name: rk
    required: true
```

Prompts call tools internally during their LLM execution. They are the user-facing templates that anchor common workflows.

---

## 7. Auth and tenant scoping

### 7.1 Session model

An MCP session is bound to:
- An **org_id** (the T0 Organization the session reads from).
- A **user_id** (the human actor; used in audit logs).
- A **role** (drives PII / sensitivity filters; defined per org).

For local stdio mode: configured at server startup via env vars `OFDY_ORG_ID`, `OFDY_USER_ID`, `OFDY_ROLE`.

For HTTP mode: bearer token at `/mcp` endpoints; token resolves to (org, user, role) via the existing auth system.

### 7.2 Cross-tenant isolation

Every tool and resource filters by `org_id` before any return. Cross-tenant access is rejected even if the caller knows the target rk. Verified by integration tests.

### 7.3 Sensitivity / PII filtering

The session's role determines which sensitivity classes are visible. Default policy:

| Role | Max sensitivity |
|---|---|
| `analyst` | `confidential` |
| `compliance_analyst` | `confidential` (PHI-allowed) |
| `data_steward` | `restricted` |
| `mcp_anonymous` | `internal` |

Filters apply at:
- `search_*` — assets above the role's max are omitted from results.
- `describe_*` — described, but PII-categorized columns have their `description` masked to `[REDACTED]` when the role doesn't have the relevant PII clearance.
- `ask` — context loader's bundle pull respects sensitivity; final answer never contains content from above-clearance assets.

### 7.4 Audit

Every tool call records:
```sql
CREATE TABLE mcp_audit (
  audit_id        bigserial PRIMARY KEY,
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  org_id          text NOT NULL,
  user_id         text NOT NULL,
  role            text NOT NULL,
  tool_name       text NOT NULL,
  tool_args_hash  text NOT NULL,
  outcome         text NOT NULL,   -- 'ok' | 'error' | 'filtered'
  wall_time_ms    integer,
  context_loaded  jsonb            -- summary of what was returned
);

CREATE INDEX idx_mcp_audit_user ON mcp_audit (org_id, user_id, occurred_at DESC);
```

Audit is queryable for compliance reviews ("what did this user ask, when, and what did the system return").

---

## 8. Performance, caching, rate limits

### 8.1 Cache policy

| Cache | TTL | Invalidation |
|---|---|---|
| Tool result by `(tool_name, hash(args), org_id)` | 5 minutes | On bundle/card writes to any referenced rk |
| Resource content by URI | 60 seconds | On underlying bundle file change (manifest sha) |
| MCP server tool listing | indefinite | On server restart |

In-process LRU; for multi-instance deployments back with Redis or rely on per-instance caches (acceptable for tens of concurrent users).

### 8.2 Rate limits

Per session:
- 30 tool calls / minute
- 5 `ask` calls / minute (LLM-bound; more expensive)
- 100 resource reads / minute

Per org:
- Configurable global cap; default 1,000 tool calls / minute aggregated across users.

429 responses on exceed; the MCP client can back off.

### 8.3 Cost accounting

Every `ask` call records token counts. Aggregations:
- Cost per org per day
- Per-user token consumption (for fair-use enforcement)
- Per-prompt-template effectiveness (which prompts produce high-quality answers vs token waste)

---

## 9. Streaming responses

For `ask` and `recommend_metrics`, streaming is supported via MCP's streaming-tool extension (SSE chunks).

Stream events:

```
event: text
data: {"delta": "Based on the available data ..."}

event: text
data: {"delta": " the Cornerstone OnDemand training tables ..."}

event: citation
data: {"rk": "postgres://acme/csod/public/csod_training_assignment", "claim_id": "..."}

event: done
data: {"citations": [...], "context_used": {...}, "wall_time_ms": 3420}
```

Non-streaming callers receive a single response with the final answer.

---

## 10. Implementation locations

```
genieml/dataservices/app/mcp/
  server.py                       # MCP server entrypoint (stdio + HTTP)
  tools/
    search_tables.py
    describe_table.py
    search_concepts.py
    describe_concept.py
    find_metrics.py
    trace_lineage.py
    find_owners.py
    find_related_assets.py
    ask.py                        # server-side Q&A agent
    compare_sources.py
    recommend_metrics.py
  resources/
    asset_resources.py
    card_resources.py
    catalog_resources.py
  prompts/
    compliance_dashboard_proposal.yaml
    causal_explainer.yaml
    data_asset_summary.yaml
  middleware/
    auth.py
    tenant_scoping.py
    rate_limit.py
    audit.py
  agents/
    qa_agent.py                   # the orchestration inside ask
```

The MCP server depends on:
- `ontology_foundry.consumer.BundleStore` and `OntologyContextLoader` (from `bundle_consumer_api_spec.md`).
- `ontology_foundry.llm` providers.

No new storage; reads existing Postgres + Qdrant + bundle files.

---

## 11. Example interactions

### 11.1 Direct user from Claude Desktop

User in Claude Desktop:
> Which tables in our CSOD source hold employee training completion data?

Claude internally calls:
1. `search_tables({ query: "employee training completion", source_id: "csod-servicenow-local" })` → returns 8 candidates.
2. `describe_table({ rk: "...", include: ["mdl", "semantic_bindings"] })` for top 3.
3. Synthesizes: "Three tables: `training_assignment` (per-employee assignments and completion dates), `learning_activity` (granular per-event progress), and `certification_core` (final certifications). The completion state is tracked in `training_assignment.completed_date`. The `employee` concept and `training_assignment` concept are both bound to these tables."

### 11.2 Server-side `ask` agent

User via custom app:
```json
{
  "tool": "ask",
  "args": {
    "question": "Which employees are at highest risk of HIPAA training non-compliance?",
    "intent_hint": "causal_reasoning"
  }
}
```

Server:
1. Anchor resolution: `[employee, training_assignment, overdue_risk, compliance_gap]`.
2. Intent: `causal_reasoning` (provided).
3. Context load: pulls full bodies for employee, training_assignment, overdue_risk; bundles for the 3 most-bound tables.
4. LLM synthesis with citation requirement.
5. Returns:

```json
{
  "answer": "Employees with elevated HIPAA training non-compliance risk are those who satisfy: (a) employment_status = 'active' AND (b) have one or more TrainingAssignments whose due_date has passed and completed_date is null (the OverdueAssignment derived state), particularly in clinical departments. The OverdueRisk causal_node aggregates these per-employee signals; departments aggregate further into the ComplianceGap node. Per the data, the relevant assets are csod_training_assignment (status field), csod_employee (employment_status), and dept_compliance_rollup (department-level rollup).",
  "citations": [
    { "rk": "postgres://...csod/public/csod_training_assignment", "kind": "table" },
    { "rk": "postgres://...csod/public/csod_employee", "kind": "table" },
    { "card_id": "overdue_risk", "kind": "causal_node" }
  ],
  "context_used": { "cards_full": 3, "cards_summary": 2, "cards_manifest": 5, "bundles_loaded": 3, "estimated_tokens": 7820 },
  "intent_used": "causal_reasoning"
}
```

### 11.3 Resource read

```
read_resource("ofdy://tenant/acme-corp/asset/postgres%3A%2F%2F...%2Fcsod_employee/mdl")
```

Returns the full `mdl.json` for the asset.

---

## 12. Test plan

| Test | Verifies |
|---|---|
| `test_search_tables_tenant_isolation` | Cross-tenant rk in args returns empty / filtered |
| `test_describe_table_role_redaction` | PII-categorized fields are masked for an `analyst` role |
| `test_ask_cites_only_loaded_context` | The synthesizer's citations all resolve to assets in `context_used` |
| `test_ask_streaming` | SSE chunks emit in order; terminal `done` event contains citations |
| `test_resources_listing_pagination` | Large tenant resources/list paginates correctly |
| `test_rate_limit_per_session` | 31st call within a minute returns 429 |
| `test_audit_writes_per_call` | Every tool invocation produces exactly one `mcp_audit` row |
| `test_prompts_compliance_dashboard_proposal` | Prompt template renders + executes against a fixture tenant |
| `test_servicenow_qa_fixture` | The ServiceNow 241-table fixture (post auto-build) answers a curated set of 10 questions with `quality >= 0.7` per the eval harness |

---

## 13. Operations

### 13.1 Local dev (stdio mode)

```bash
# In ~/.config/claude/config.json or similar
{
  "mcpServers": {
    "ontology-foundry": {
      "command": "python",
      "args": ["-m", "genieml.dataservices.app.mcp.server", "--transport", "stdio"],
      "env": {
        "OFDY_ORG_ID": "acme-corp",
        "OFDY_USER_ID": "dev@acme.com",
        "OFDY_ROLE": "data_steward"
      }
    }
  }
}
```

Claude Desktop discovers tools/resources/prompts on session start.

### 13.2 Hosted (HTTP mode)

```bash
python -m genieml.dataservices.app.mcp.server \
  --transport http \
  --host 0.0.0.0 --port 8765 \
  --auth-backend identity-provider:acme-sso
```

MCP clients connect with bearer tokens; the auth backend resolves to (org, user, role).

### 13.3 Disabling tools per tenant

Per `tenants/<org_id>/mcp_config.yaml`:

```yaml
mcp:
  enabled: true
  enabled_tools:                       # whitelist; omit a tool to disable it
    - search_tables
    - describe_table
    - search_concepts
    - describe_concept
    - find_metrics
    - trace_lineage
    - find_owners
    - ask                              # the heavy LLM tool
  disabled_resources: []
  rate_limits:
    per_session_per_minute: 30
    ask_per_session_per_minute: 5
```

Tenants can disable `ask` if they prefer client-driven orchestration only.

---

## 14. Open items

- **Multi-user sessions in stdio mode** — currently single (env-var bound). For team use in shared environments, a hybrid stdio-with-token mode could work; defer.
- **Streaming over stdio** — MCP's stdio transport doesn't natively stream; SSE works over HTTP. For stdio, `ask --stream=false` is the only supported option in v1.
- **Tool versioning** — when a tool's input/output schema evolves, how do clients negotiate compatibility? MCP doesn't fully solve this; we version tool names (`search_tables_v2`) when breaking. Lean on tooling once mature.
- **Server-side prompt caching** — frequently-invoked `ask` calls with similar context can cache the `OntologyContextLoader` output. Defer until cost measurements justify.
- **Federated MCP** — when an org has multiple foundry instances (per-region), a single MCP entry point that fans out. Defer.

---

## 15. Cross-spec amendments (deferred)

| Spec | Section | Change |
|---|---|---|
| `bundle_consumer_api_spec.md` | §13 | Note MCP server is a consumer; the `BundleStore` Protocol is the read surface MCP uses. |
| `T0_T1_organization_source_spec.md` | §4 | MCP role-based filtering is a downstream consumer of the governance posture. |

Apply when implementation lands.

---

## 16. Change log

| Date | Change |
|---|---|
| 2026-05-16 | Initial draft. MCP server + 11 tools + 6 resources + 3 prompts + server-side `ask` agent. |
