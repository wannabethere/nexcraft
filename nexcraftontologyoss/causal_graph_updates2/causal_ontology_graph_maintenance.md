# Graph Materialization and Maintenance

How the runtime graphs that serve KnowQL queries and provide context to the
LLM stay synchronized with the underlying card store. The cards are the
source of truth; the graphs are derived views that exist for traversal and
context performance.

---

## 1. Purpose and Scope

When the ingestion pipeline finishes, every fact in the ontology lives as a
knowledge card in Qdrant — readable, versioned, embedded. But card prose is
poorly suited to graph traversal: "find every causal_edge that points into
ComplianceGap with weight > 0.3" should not require fetching every card and
parsing its header.

To serve those queries, the pipeline maintains five derived graphs alongside
the card store. They are materialized views: cheap to query, expensive to
build, and updated incrementally as cards change. The cards remain
authoritative — if a graph and a card disagree, the card wins, and the graph
is rebuilt to match.

This plan covers:

- The five graphs the system maintains and what each holds.
- How cards become nodes and edges (construction).
- How card edits propagate to graph updates (synchronization).
- Versioning, snapshots, and rollback.
- Storage choices and indexing.
- Pruning, cleanup, and operational hygiene.

---

## 2. The Five Graphs

Different card families produce different graphs, each with its own
structure, access pattern, and update characteristics.

| Graph                | Built from                                          | Structure         | Update cadence     |
| -------------------- | --------------------------------------------------- | ----------------- | ------------------ |
| **Semantic graph**   | `object_type`, `link_type`, `property_type`, `interface` cards | General graph (cyclic OK) | Daily / on card edit |
| **Causal graph**     | `causal_node`, `causal_edge`, `causal_rule` cards   | DAG (no cycles)   | Daily / on weight learn |
| **Lineage graph**    | `lineage_edge` cards                                | Append-only DAG   | Per pipeline run   |
| **Concept hierarchy**| `concept` cards with parent_concepts                | Tree (mostly) / DAG | Daily / on card edit |
| **Governance graph** | `role`, `permission`, `marking` cards               | Bipartite tripartite | On governance card edit |

The graphs share node identities — a `causal_edge` references `object_type`
nodes that also live in the semantic graph; a `lineage_edge` may point to
both raw rows and derived values that exist as semantic nodes. The shared
identity is what makes cross-graph queries possible.

### 2.1 Semantic graph

Nodes: object types, properties (attached as node attributes), interfaces.
Edges: link_types, with `derivation` as edge label (`structural`, `temporal`,
`derived`, `causal`, `governance`). Cyclic — a manager is an employee who
supervises other employees, which is a self-loop on Employee.

Used by: KnowQL `MATCH` patterns, schema reasoning, neighborhood lookups
during card generation.

### 2.2 Causal graph

Nodes: causal_nodes. Edges: causal_edges with their weights, CIs,
identifiability flags as edge properties. Strict DAG — cycle detection runs
on every update and rejects cycle-creating edges.

**Stored authoritatively: direct edges only.** No transitive closure, no
materialized multi-hop paths. A 3-hop path from CourseDesign to ComplianceGap
exists in the graph as three separate edges; the path is composed at query
time, not stored.

This is a deliberate choice. Storing transitive edges explicitly would
multiply the edge count quadratically without adding information — they're
all derivable from the direct edges. Worse, stored multi-hop edges would
risk drifting from their constituents when direct edges update. Compute on
demand keeps the corpus small and the truth in one place.

**Default reasoning depth: 3.** KnowQL causal queries default to traversing
three hops from the target node. This matches what humans hold in working
memory, keeps query latency predictable, and bounds compounding uncertainty.
Direct edges with weight 0.6 ± 0.05 become 3-hop paths with weight ~0.22 and
substantially wider CIs — depth 3 is roughly the limit at which path
estimates remain interpretable.

**Multi-hop paths are computed and cached.** When a depth-3 traversal runs
for a high-traffic target node, the result is cached in Redis with TTL.
Pre-computed depth-3 subgraphs for the top-20 most-queried nodes (typically
ComplianceGap, OverdueRisk, PhishingRisk for LMS+security; AdverseEventRisk,
ProtocolDeviation for eClinical) are refreshed nightly. Hot-path queries hit
cache; cold-path queries traverse from direct edges and warm the cache for
next time.

**Path computation honors compounding uncertainty.** A path's weight is
not a simple product of edge weights; it's computed via Shapley attribution
over the noisy-OR functional form when edges combine, with bootstrap-
propagated CIs. Responses surface this — *"the magnitude estimate at depth
3 is less precise than for direct causes"* — rather than presenting
compounded estimates as if they had direct-edge precision.

**Progressive expansion.** When a query reaches the depth-3 boundary, the
maintainer identifies boundary nodes and computes "expansion offers" — for
each boundary node, the count of additional causal edges available beyond
it and a one-sentence preview. These are cheap to compute (a single hop
beyond the boundary) and let the response synthesizer surface them as
options without traversing.

Used by: causal effect computation (depth-bounded), counterfactual
simulation, attribution queries (Shapley over depth-3 paths),
identifiability checking, expansion-offer generation.

### 2.3 Lineage graph

Nodes: derived values and source rows. Edges: production relationships
(this value was produced from these inputs by this rule). Append-only — old
edges are never modified, only superseded by newer edges.

Used by: provenance queries (`EXPLAIN`), audit, and forensic investigation
of "where did this value come from".

### 2.4 Concept hierarchy

Nodes: concept cards. Edges: parent-child relationships from
`parent_concepts` in card headers. Mostly a tree, occasionally a DAG when a
concept has multiple parents (e.g., "PrivilegedRoleTraining" parents to both
"PrivilegedAccess" and "MandatoryTraining").

Used by: concept-similarity search, domain reasoning, abstraction-level
filtering during retrieval.

### 2.5 Governance graph

Nodes: roles, permissions, object types, actions, markings. Edges: grants
(role → permission), scopes (permission → object_type/action), markings
(object_type → marking).

Used by: every query that has to filter results by access. Hot path; cached
aggressively.

---

## 3. Construction Strategy

Three options were considered for how the graphs come into existence. The
chosen approach is the third.

| Approach              | Pro                              | Con                                  |
| --------------------- | -------------------------------- | ------------------------------------ |
| Eager full rebuild    | Simple, always consistent         | Expensive, locks during rebuild      |
| Pure lazy on-query    | Cheapest at rest                  | Cold queries are slow; cache-warming complexity |
| **Incremental maintenance with periodic rebuilds** | Fast queries, bounded staleness  | Most complex of the three            |

The pipeline maintains graphs incrementally on every card change and runs a
full rebuild weekly to catch any drift. The weekly rebuild is the safety
net; the incremental updates are the steady-state path.

### 3.1 Bootstrap (first-run construction)

When a tenant is first onboarded, the entire card corpus exists but no
graphs do. Bootstrap walks the corpus once per graph type and emits all
nodes and edges:

```
1. Scan ontology_semantic_objects → emit semantic graph nodes
2. Scan ontology_semantic_links   → emit semantic graph edges
3. Scan ontology_semantic_causal_nodes → emit causal graph nodes
4. Scan ontology_semantic_causal_edges → emit causal graph edges
   ↳ Run cycle check; reject any cycle-creating edges with HITL escalation
5. Scan ontology_lineage          → emit lineage graph (append-only)
6. Scan ontology_semantic_concepts → emit concept hierarchy
7. Scan ontology_dynamic          → emit governance graph
8. Build secondary indices (see §6)
9. Snapshot: graph_state_v0
```

Bootstrap is a one-time cost; subsequent runs use incremental updates from
this baseline.

### 3.2 Incremental updates

Every card edit emits a `card_event` (created, updated, deprecated). The
graph maintainer subscribes to these events and applies the corresponding
graph mutation:

| Card event                         | Graph effect                                       |
| ---------------------------------- | -------------------------------------------------- |
| `object_type` created              | Add semantic node                                  |
| `object_type` updated              | Update node attributes; if refs changed, re-evaluate edges |
| `object_type` deprecated           | Mark node as deprecated; cascade-flag dependent edges |
| `link_type` created                | Add semantic edge                                  |
| `causal_edge` created              | Cycle-check, then add edge with weight properties  |
| `causal_edge` weight updated       | Update edge property only — no structural change   |
| `lineage_edge` created             | Append to lineage graph                            |
| `concept` parent changed           | Move node in hierarchy                             |
| `permission` added to role         | Add edge in governance graph                       |

The graph mutation is the *minimal* change implied by the card event — never
a wholesale recompute. This is what keeps incremental updates cheap.

### 3.3 Weekly rebuild

Once a week, a full rebuild runs in shadow mode:

```
1. Construct fresh graphs from the current card corpus.
2. Diff against the live incremental graphs.
3. If diff is empty: no action, log "graph drift = 0".
4. If diff is non-empty:
   a. Promote shadow graphs to live (atomic swap).
   b. Log diff details for forensics.
   c. Alert if diff exceeds threshold (something is wrong with incremental).
```

Shadow mode means the rebuild does not block live queries. Drift is
expected to be near-zero in a well-behaved pipeline. Non-zero drift is
investigated.

---

## 4. Update Propagation

The map from card change to graph change is rarely one-to-one. A single
card edit can ripple through multiple graphs.

### 4.1 The propagation rules

For each card kind, an explicit propagation rule lists which graphs it
affects and what the update looks like:

| Card kind         | Affects                                    | Specific updates                                  |
| ----------------- | ------------------------------------------ | ------------------------------------------------- |
| `object_type`     | Semantic, governance (if marking changed)  | Node attrs; cascade to incident edges if refs changed |
| `link_type`       | Semantic                                   | Edge add/update; structural integrity check       |
| `property_type`   | Semantic (as node attr)                    | Attribute update on parent object_type node       |
| `interface`       | Semantic (multi-node)                      | Update implementing object_type nodes             |
| `concept`         | Concept hierarchy, semantic (back-refs)    | Move/add in tree; update referencing object_types |
| `causal_node`     | Causal                                     | Node add/update                                   |
| `causal_edge`     | Causal                                     | Edge add/update with cycle check                  |
| `causal_rule`     | Causal (activation metadata)               | Edge metadata update                              |
| `derivation_rule` | Semantic (creates derived nodes/edges)     | Add derived nodes if rule fires                   |
| `validation_rule` | None (pure function)                       | No graph effect — validation is at write time     |
| `action_type`     | Governance                                 | Node add; bind to permissions                     |
| `function`        | None (pure function)                       | No graph effect                                   |
| `marking`         | Governance, semantic (propagation)         | Node add; transitive marking applied to refs      |
| `role`            | Governance                                 | Node add; permission edges                        |
| `permission`      | Governance                                 | Node add; scope edges                             |
| `audit_entry`     | None (event log only)                      | No graph effect                                   |
| `lineage_edge`    | Lineage                                    | Append edge                                       |

These rules are themselves stored as configuration cards in the Kinetic
Layer, so they can be updated without redeploying the maintainer.

### 4.2 Cascade depth

When a card edit triggers a graph update, the update sometimes cascades. A
deprecated `object_type` card flags every link_type pointing to it as
needing review; a deleted concept removes child concepts that have no other
parent. The cascade is bounded:

- **One hop** for routine updates (most edits).
- **Two hops** for refactoring edits (interface changes, marking changes).
- **Full subgraph** for structural changes (object_type deletion) — these
  are HITL-gated and rare.

Any cascade beyond one hop is logged at WARN with the triggering card and
affected node/edge IDs so reviewers can sanity-check.

### 4.3 Conflict resolution

Two card edits in the same run can produce conflicting graph mutations —
e.g., one edit adds a causal edge, another removes its target node.
Resolution rules in priority order:

1. **Card timestamp**: later edit wins for property updates.
2. **Structural integrity**: an edge cannot exist without its endpoints; if
   one card removes a node and another adds an edge to it, the edge is
   rejected with an audit entry.
3. **HITL escalation**: any unresolvable conflict pages the on-call
   reviewer with both edits and the proposed resolution.

---

## 5. Consistency Model

Eventual consistency between cards and graphs, with bounded staleness.

### 5.1 The contract

- **Cards are strongly consistent** — Qdrant writes are committed before
  the maintainer receives the event.
- **Graphs are eventually consistent** — typically within seconds of the
  card commit, never more than 60 seconds in steady state.
- **Queries declare their tolerance**: KnowQL queries can request
  `STRICTLY_CURRENT` (waits for graph to catch up if behind), `RECENT`
  (default — accepts up to 60s staleness), or `BEST_EFFORT` (returns
  whatever's there).

### 5.2 Why not strong consistency

Strong consistency would require synchronous graph updates on every card
write, which would cap card-write throughput at the slowest graph mutation.
For a pipeline doing 50 card writes per minute during ingestion peaks,
this is a real cost. Eventual consistency with bounded staleness gives the
pipeline room to batch graph updates while keeping query results fresh
enough for almost all uses.

The exceptions — when strong consistency genuinely matters — are rare
(causal effect estimation right after a weight refit, governance checks
right after a permission change). Those queries opt in to
`STRICTLY_CURRENT` and pay the latency cost.

### 5.3 Detecting drift

Two checks run continuously:

**Per-card drift check.** A sampled fraction of cards (default 1%) is
compared against the graph: every ref in the card must have a
corresponding node, every causal_edge weight in the card must match the
graph property. Discrepancies trigger an alert and a targeted rebuild for
that card's neighborhood.

**Whole-graph drift check.** The weekly rebuild's diff is the canonical
drift measurement. A diff above 0.1% of nodes/edges is a real bug.

---

## 6. Storage and Indexing

### 6.1 Storage choices

The graphs are stored separately from the card vector store but share an
identity space (card IDs are graph node IDs).

| Graph                | Backend                                | Why                                                  |
| -------------------- | -------------------------------------- | ---------------------------------------------------- |
| Semantic             | Postgres (recursive CTE) + NetworkX cache | Cyclic graph, moderate size, joins with cards needed |
| Causal               | Postgres + in-memory NetworkX          | DAG, smaller, traversed often, needs fast cycle checks |
| Lineage              | Postgres (append-only table)           | Append-only, large, time-windowed queries dominate    |
| Concept hierarchy    | Postgres + in-memory tree              | Tree-shaped, small, hot path                          |
| Governance           | Postgres + in-memory cache             | Small, queried on every request, must be fast         |

The choice of Postgres rather than a dedicated graph database is
deliberate: at the working scale (low thousands of nodes per graph per
tenant), recursive CTEs are fast enough, and avoiding a second database
keeps operations simpler. KuzuDB or Neo4j become attractive only when a
single graph exceeds ~10k edges and traversal queries dominate.

In-memory caches sit in front of Postgres for hot paths. NetworkX (Python)
or rustworkx (faster, drop-in compatible) for the application layer. The
cache is invalidated by card events.

### 6.2 Schema sketch

```sql
-- Generic node table, partitioned by graph type
CREATE TABLE graph_nodes (
  graph_type    TEXT NOT NULL,    -- 'semantic', 'causal', 'lineage', etc.
  node_id       TEXT NOT NULL,    -- card_id
  card_version  INTEGER NOT NULL, -- which version of the card this reflects
  attributes    JSONB,            -- denormalized header fields for fast filtering
  status        TEXT NOT NULL,    -- 'live', 'deprecated', 'pending'
  created_at    TIMESTAMPTZ,
  updated_at    TIMESTAMPTZ,
  PRIMARY KEY (graph_type, node_id)
);

-- Generic edge table, partitioned by graph type
CREATE TABLE graph_edges (
  graph_type    TEXT NOT NULL,
  edge_id       TEXT NOT NULL,
  source_node   TEXT NOT NULL,
  target_node   TEXT NOT NULL,
  edge_kind     TEXT,             -- 'structural', 'causal', 'lineage', etc.
  attributes    JSONB,            -- weight, CI, identifiability, etc.
  status        TEXT NOT NULL,
  created_at    TIMESTAMPTZ,
  PRIMARY KEY (graph_type, edge_id)
);

-- Reverse-reference index for fast neighbor lookup
CREATE INDEX idx_edges_target ON graph_edges (graph_type, target_node);
CREATE INDEX idx_edges_source ON graph_edges (graph_type, source_node);

-- Snapshot table for versioning (see §7)
CREATE TABLE graph_snapshots (
  snapshot_id   TEXT PRIMARY KEY,
  taken_at      TIMESTAMPTZ NOT NULL,
  card_corpus_hash TEXT NOT NULL,
  node_count    INTEGER,
  edge_count    INTEGER,
  metadata      JSONB
);
```

### 6.3 Secondary indices

Built on top of the primary tables to accelerate common queries:

- **Reverse-reference index**: for any node, find all edges pointing at it.
  Materialized as a Postgres index; used during card generation to find
  neighbors and during cascade resolution.
- **Causal-edge by target**: ordered by weight magnitude, used by
  attribution queries.
- **Lineage by derived value**: time-ordered, used by `EXPLAIN` traversals.
- **Concept-similarity index**: separate from the graph; lives in Qdrant
  alongside the concept cards but indexed by graph node IDs.

---

## 7. Versioning and Snapshots

### 7.1 Per-graph versioning

Every graph has a version counter that increments on each mutation. Card
events carry the card version; the resulting graph mutations carry the
graph version that resulted. This lets queries pin to a specific graph
version when reproducibility matters.

### 7.2 Snapshots

Daily snapshots capture the full graph state. Snapshots are cheap because
the graph is small (low thousands of nodes/edges per tenant) and stored
compactly:

```
snapshots/<tenant>/<graph_type>/<date>.parquet
```

Each snapshot includes the full node and edge sets plus the
card_corpus_hash that produced it. Reconstructing a historical graph for
forensics is then a single file read.

### 7.3 Rollback

Two rollback paths:

- **Card-driven rollback.** A bad card version is reverted via the card
  store; the maintainer receives the resulting card event and propagates
  the graph mutation backward. This is the normal path.
- **Graph-driven rollback.** If incremental updates have introduced
  systematic drift, the daily snapshot is restored as the live graph and
  the incremental log replays from the snapshot. This is the disaster
  path; it's existed only as a fire drill so far.

Both paths produce audit entries.

---

## 8. Query Patterns and Caching

### 8.1 Hot patterns

The patterns that dominate query traffic, in order:

1. **One-hop neighborhood fetch.** "Give me everything connected to
   training_assignment." Used during card generation context loading and
   during retrieval.
2. **Depth-3 causal subgraph for a target node.** "Give me three hops of
   causes for ComplianceGap with weight > 0.2." Used by KnowQL `CAUSAL
   EFFECT` and `WHAT-IF` queries; this is the most common causal access
   pattern given the depth-3 default.
3. **Causal expansion from a boundary node.** "Now expand from
   TimeManagement three more hops." Triggered by progressive expansion
   when users ask follow-up questions.
4. **Permission filter.** "Can role X see object_type Y?" Used on every
   query.
5. **Lineage trace.** "Walk back from this derived value to source rows."
   Used in EXPLAIN queries.
6. **Concept ancestors.** "What parent concepts does PhishingRisk roll up
   to?" Used in concept-similarity retrieval.

### 8.2 Caching strategy

Each pattern has a specific cache:

| Pattern                       | Cache layer              | Invalidation                          |
| ----------------------------- | ------------------------ | ------------------------------------- |
| One-hop neighborhood          | Redis, keyed by node_id  | On any edit to node or its edges      |
| Depth-3 causal subgraph       | Redis, keyed by target node + depth + filter | On any causal_edge weight update within the subgraph's reach |
| Boundary expansion offers     | Redis, keyed by boundary node | On edits to nodes one hop beyond boundary |
| Permission filter             | In-memory, per app instance | On any governance card edit           |
| Lineage trace                 | Postgres materialized view | Refreshed weekly (lineage is append-only) |
| Concept ancestors             | In-memory tree           | On any concept card edit              |

Cache invalidation is event-driven: card events fan out to the relevant
caches, which invalidate the affected entries. Stampede protection on
cache misses uses single-flight patterns.

The depth-3 cache is the most active. For high-traffic target nodes
(ComplianceGap, OverdueRisk, etc.), the cache is warm essentially always;
each cache hit avoids a graph traversal that would otherwise touch
hundreds of edges. Cache invalidation is targeted — when a `causal_edge`
weight updates, only subgraphs containing that edge are invalidated, not
all subgraphs.

### 8.3 Pre-computed traversals

Some queries are run thousands of times per day with identical inputs —
the permission filter for the most common roles, the causal subgraph for
ComplianceGap and OverdueRisk. These get pre-computed traversals
maintained alongside the graphs:

```
SELECT pre_computed.permitted_object_types
FROM precomputed_role_permissions
WHERE role = 'compliance_analyst' AND graph_version = $current
```

For depth-3 causal subgraphs, a nightly job pre-computes subgraphs for the
top-20 most-queried target nodes per tenant. The list is determined from
query log analysis — nodes that appeared as causal targets in 50+ queries
in the prior week get pre-computation. The pre-computed subgraph includes
the depth-3 traversal results, the Shapley attributions, and the
expansion offers for boundary nodes.

A nightly refresh keeps these traversals current. Query traffic for these
patterns skips the live graph entirely.

---

## 9. Pruning and Cleanup

The graphs grow. Without active management, deprecated nodes accumulate,
old causal edges with weight near zero pollute attribution queries, and
the lineage graph balloons.

### 9.1 Pruning rules

| What                                               | Cadence  | Action                                        |
| -------------------------------------------------- | -------- | --------------------------------------------- |
| Deprecated nodes (>90 days, no active references)   | Weekly   | Mark for removal; HITL approve; delete        |
| Causal edges with weight < 0.05 and CI excluding 0 below 0.1 | Monthly  | Mark as inactive; keep for history; exclude from queries by default |
| Lineage edges older than retention window           | Daily    | Move to cold storage; queryable but slow      |
| Hypothesized causal edges never promoted (>180 days, n < 100) | Monthly | HITL review; either promote or deprecate     |
| Orphaned nodes (no incident edges, no card refs)    | Weekly   | Flag; remove if confirmed orphan after one week |

Pruning is conservative. Removing a node from the graph does not delete
the underlying card — the card stays in Qdrant (versioned, searchable)
and can be re-promoted if needed. The graph is the working set; the
cards are the archive.

### 9.2 Retention policy

Lineage is the largest growth source. Default retention:

- **Hot**: last 90 days, fully indexed in Postgres.
- **Warm**: 90 days to 1 year, archived to compressed Parquet, queryable
  with seconds-to-minutes latency.
- **Cold**: >1 year, archived to S3, queryable with restore process.

Compliance and audit requirements may extend hot retention. The retention
policy is itself a configuration card so it can be updated without code
changes.

---

## 10. Multi-Tenant Considerations

Each tenant has its own copy of every graph. They are not shared.

### 10.1 Why not shared

Sharing graphs across tenants would require namespacing every node ID and
adding tenant filters to every query. The performance and complexity costs
are real, and the compliance story is fraught — a tenant's causal weights
are derived from their data and can leak business information. Per-tenant
graphs avoid all of this.

### 10.2 Shared infrastructure

The maintainer service is shared. It services all tenants and routes
events to the right graph. Each tenant gets:

- A distinct schema in Postgres (or row-level multi-tenancy with strict
  tenant filters at every query).
- A distinct prefix in Redis (`graph:<tenant>:<key>`).
- A distinct Qdrant collection set (already designed in the cards plan).
- A distinct snapshot directory.

### 10.3 Cross-tenant analytics (optional, opt-in)

Some queries — typically for benchmarks or industry comparisons — require
data across tenants. These use a separate aggregated store, populated
from anonymized snapshots, and do not use the live tenant graphs. Opt-in
per tenant; off by default.

---

## 11. Tooling

| Need                                | Recommendation                                       |
| ----------------------------------- | ---------------------------------------------------- |
| Graph storage (relational)          | Postgres with appropriate indexes                    |
| In-memory graph (Python app layer)  | `rustworkx` (faster than NetworkX, same API)         |
| Caching                             | Redis for hot one-hop and subgraph caches            |
| Event bus (card events → maintainer) | NATS / Kafka / Redis Streams                         |
| Cycle detection                     | rustworkx topological sort with cycle catch          |
| Snapshot format                     | Parquet for graph state, JSON for metadata           |
| Snapshot diff                       | Custom — graph-aware, kind-aware diff                |
| Drift monitoring                    | Prometheus metrics, Grafana dashboards               |
| Optional dedicated graph DB         | KuzuDB (embedded) or Neo4j when scale demands it     |

---

## 12. Operational Hygiene

A short list of practices that keep the system healthy:

- **Run the weekly rebuild even when nothing seems wrong.** The drift
  number is a leading indicator — small and growing means an incremental
  rule is buggy.
- **Alert on incremental update lag.** If the maintainer is more than 60
  seconds behind card writes, something is wrong upstream.
- **Audit cascade-depth distributions.** A sudden uptick in two-hop or
  full-subgraph cascades usually means a refactoring edit is in flight;
  worth confirming it was intentional.
- **Track cache hit rates.** A drop in one-hop hit rate indicates either
  a workload shift (new query patterns) or invalidation thrashing.
- **Periodically restore a snapshot in staging.** Disaster path needs to
  be exercised before it's needed.

---

## 13. Open Design Questions

1. **In-memory vs persistent graph.** For the causal and concept graphs
   (small, often-read), keeping a fully in-memory copy in each app
   instance might be cleaner than Redis-fronted Postgres. The cost is
   coordinating invalidation across instances. Probably move to in-memory
   when query volume justifies the operational cost.

2. **Snapshot frequency.** Daily snapshots are the working answer. A
   high-volume tenant with rapid card edits might benefit from hourly
   snapshots; a quiet tenant probably only needs weekly. Per-tenant
   snapshot policy as a configuration card.

3. **Graph database adoption threshold.** Postgres works to ~10k edges
   per graph. Beyond that, traversal queries (especially causal subgraph
   queries with depth > 3) get expensive. The threshold for moving the
   causal subgraph to KuzuDB or Neo4j is probably around 10k edges and
   sustained query latency above 200ms p95.

4. **Lineage retention.** 90-day hot is generous and may be too generous
   for high-throughput tenants. Compliance requirements vary by industry;
   the retention card pattern lets policy adapt without code changes,
   but defaults need attention.

5. **Cross-graph query optimization.** Queries that span semantic and
   causal graphs (common: "what's the causal effect of X on Y, where X
   and Y are object types") currently do two queries and join in app
   code. A query planner that understands both graphs and pushes joins
   down might be worth building once query volume is high enough.

6. **Hot-path graph in WASM.** For very latency-sensitive paths
   (permission filter on every API request), compiling the relevant
   subgraph to a WASM module that ships with the app is a real option.
   Adds a build step but cuts latency to single-digit microseconds.

---

## 14. What Ships First

A staged delivery plan, similar in shape to the ingestion plan:

**Phase 1 — Bootstrap.** Construct semantic and concept graphs from the
existing cards. No incremental updates yet; weekly full rebuilds.
Postgres-only, no caching layer. End: KnowQL `MATCH` queries work.

**Phase 2 — Causal and lineage.** Add causal graph with cycle detection
and lineage append-only graph. Still no incremental updates. End: causal
effect and `EXPLAIN` queries work.

**Phase 3 — Incremental updates.** Add the maintainer service consuming
card events. Reduce rebuild cadence to weekly drift check. End: graphs
stay fresh in seconds, not days.

**Phase 4 — Caching.** Add Redis for one-hop and subgraph caches.
In-memory caches for governance and concept hierarchy. End: hot-path
queries go from tens of milliseconds to sub-millisecond.

**Phase 5 — Multi-tenant hardening.** Per-tenant isolation in storage,
caching, and snapshots. End: production-ready multi-tenant deployment.

**Phase 6 — Cross-graph optimization.** Query planner that pushes joins
across graphs. Snapshot diff tooling. WASM for permission hot paths if
latency demands it. End: optimized for scale.

Each phase is end-to-end shippable. Phases 1–3 are the critical path; 4–6
are optimizations that pay off at scale.
