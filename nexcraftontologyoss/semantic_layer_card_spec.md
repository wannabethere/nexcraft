# Semantic Layer — Card Format Specification

**Status:** Draft 2026-05-15.
**Part of:** Data Knowledge Hierarchy series.
**Depends on:** `T0_T1_organization_source_spec.md` (org-scoped namespacing).
**Forward refs:** `T2_to_T6_amundsenrds_sidecar_spec.md`, `mdl_bundle_spec.md` (bindings consume cards).
**Leverages:** `ontology_foundry/eval/gates.py` (`gate_nonempty_body`, `gate_id_pattern`, `gate_refs_resolve`); `ontology_foundry/eval/causal_checks.py`; `ontology_foundry/models.py` (`ClaimArtifact`, `RelationArtifact`).

---

## 1. Scope

This spec defines the **Semantic Layer** — the authored, narrative, typed-card representation of an organization's ontology. Cards encode object types, interfaces, causal nodes, derived states, actions, metrics, and events. They are the LLM-readable canonical form; their structured companion (`semantic_bindings.json` per asset, specified separately) is the machine-readable bridge to MDL.

This spec does **not** cover:
- MDL physical-asset descriptions (see `mdl_bundle_spec.md`).
- The card ↔ MDL bridge structure (see `mdl_bundle_spec.md` §`semantic_bindings.json`).
- Causal extraction algorithms (see `ontology_foundry/causal/`, `relations/`).
- Pack vs tenant card overlay resolution (existing `causal_ontology_foundry_design.md` §`pack_overrides_dir`).

---

## 2. Card format

A card is a single file with YAML frontmatter and Markdown body:

```markdown
---
id: employee
layer: semantic
kind: object_type
version: 3
extends: [trainable, auditable]
markings: [contains_pii]
refs: [department, role, manager, training_assignment]
---
An Employee is a person who works at the organization. ...
```

### 2.1 File location

Cards are filesystem-resident, one card per file, named `<id>.card.md`. Layout per tenant:

```
tenants/<org_id>/semantic_layer/
  object_types/<id>.card.md
  interfaces/<id>.card.md
  causal_nodes/<id>.card.md
  derived_states/<id>.card.md
  actions/<id>.card.md
  metrics/<id>.card.md
  events/<id>.card.md
```

Directory is chosen by `kind`. The `kind` value in frontmatter and the parent directory must agree (validated; see §10).

### 2.2 Required frontmatter fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | slug | ✓ | Stable identifier. snake_case. Unique within (`layer`, `kind`, `tenant`) tuple. Must match `^[a-z][a-z0-9_]*$`. |
| `layer` | enum | ✓ | `semantic` for the layer this spec governs. (Reserved values for forward-compat: `physical`, `governance`, `pack`.) |
| `kind` | enum | ✓ | See §3 taxonomy. |
| `version` | integer | ✓ | Per-card monotonic version. Starts at `1`. Bumped on every save. |
| `extends` | slug[] | per-kind | Interface ids this card implements. See §3.2. |
| `markings` | enum[] | — | Sensitivity / governance labels. Propagate to bound MDL fields. See §6. |
| `refs` | slug[] | recommended | Outbound graph edges to other cards. Every entry must resolve (gate). See §5. |

### 2.3 Optional frontmatter fields

| Field | Type | Use |
|---|---|---|
| `aliases` | string[] | Alternative names the LLM may encounter in text or queries. Used by NER pipeline. |
| `deprecated` | bool | When `true`, card is preserved but excluded from default retrieval. |
| `superseded_by` | slug | Points to the replacement card after deprecation. |
| `origin` | enum | `pack` \| `tenant` \| `derived`. Default `tenant`. Pack cards are read-only here. |
| `last_validated_at` | date | When validation gates last passed clean. Updated by the CI gate runner. |
| `notes` | string | Free-form maintenance comments not for LLM consumption. Excluded from embedding text. |

### 2.4 Body conventions

Markdown body is **canonical for the LLM**. Conventions, not hard rules:

- Open with a one-sentence definition. (`An Employee is a person who works at the organization.`)
- Describe attributes/fields in prose, naming each field in code-style backticks where helpful. (`Each employee is identified by an employee_id ...`)
- Describe constraints/invariants in declarative sentences. (`Only active employees can be assigned new training — this is enforced as a precondition on the AssignTraining action.`)
- For object_types: describe identity, attributes, constraints, source mapping, interface implementations, and causal participation. (The example in §2 is the template.)
- For interfaces: describe the contract (what an implementing object_type gains/promises) and any default refs the interface contributes.
- For causal_nodes: describe what the node measures/represents, what feeds in (subject types), what comes out (downstream effects), and the mechanism in plain language.
- For derived_states: describe the conditions under which the state attaches, and what objects/refs it attaches to.
- For actions: describe preconditions, effects, and the object_types it operates on.
- For metrics: describe what is measured, the unit, the time grain, and the primary asset/object_type it computes over.
- For events: describe the trigger, the participating object_types, and any state transitions implied.

Body should be *self-contained* for the card's scope: a reader who has only this card and its frontmatter should understand the entity. Cross-references go through `refs` (machine-checked) and prose mentions (human-readable).

---

## 3. Card kind taxonomy

### 3.1 Locked kinds

| `kind` | Purpose | Frontmatter extensions |
|---|---|---|
| `object_type` | A semantic entity. The noun. | `extends`, `refs` |
| `interface` | A composable trait `object_type`s can implement via `extends`. | `contributes_refs` (refs implementers inherit) |
| `causal_node` | A node in the causal graph; consumes subject states, produces effects. | `subject_refs` (object_types feeding in), `outcome_refs` (object_types affected) |
| `derived_state` | A computed/conditional state attached to one or more object_types. | `attaches_to` (object_type[]), `condition_summary` (one-line) |
| `action` | An operation with preconditions and effects. | `operates_on` (object_type[]), `preconditions_summary`, `effects_summary` |
| `metric` | A semantic-layer measurement. | `primary_object_type`, `grain`, `unit`, `default_time_grain` |
| `event` | A discrete occurrence in time, often a state transition. | `participants` (object_type[]), `triggers` (action_id[]) |

### 3.2 Interface semantics (`extends`)

An object_type `extends: [trainable, auditable]` inherits, from each named interface:

- **Required refs.** If `trainable` declares `contributes_refs: [training_assignment]`, the implementing object_type's effective `refs` includes `training_assignment` even if not listed locally.
- **Markings.** Markings declared on the interface propagate to implementers. (Example: an `audited_entity` interface could mark its implementers `audit_logged`.)
- **Contracts (prose).** The interface's body describes invariants implementers promise to uphold. The LLM relies on this for behavioral reasoning.

Interfaces themselves may extend other interfaces. Cycles in interface extension are invalid (validation gate).

### 3.3 Reserved frontmatter values

The system reserves these for future kinds; do not author with these `kind` values yet: `policy`, `rule`, `pipeline`, `dataset_role`, `claim`, `binding`.

---

## 4. Identifiers and namespacing

### 4.1 Tenant scoping

A card's full identity is `(tenant=<org_id>, layer, kind, id)`. Two tenants may have cards with the same `(layer, kind, id)` — they are distinct cards, distinct content, distinct versions.

### 4.2 Pack overlays

Cards shipped by a platform pack (`origin: pack`) live in a separate filesystem tree and are read-only at the tenant level. A tenant card with the same `(layer, kind, id)` **overrides** the pack card. Resolution order (existing per `causal_ontology_foundry_design.md` §`pack_overrides_dir`):

1. Tenant card (if present) wins.
2. Else, pack card.
3. Else, dangling — refs to it fail to resolve.

### 4.3 Naming rules (gates enforce)

- `id` matches `^[a-z][a-z0-9_]*$`. Enforced by `gate_id_pattern(card_id, pattern=r"^[a-z][a-z0-9_]*$")`.
- `id` is unique within `(tenant, layer, kind)`.
- `id` is stable across versions — version is for content drift, not rename.

Renaming a card requires:
1. Marking the old card `deprecated: true`, `superseded_by: <new_id>`.
2. Creating the new card with the new `id`.
3. Migrating refs at consumers (handled by the foundry's ref-migration utility, not in scope here).

---

## 5. Refs — outbound graph

### 5.1 Resolution

Every entry in `refs[]`, `subject_refs[]`, `outcome_refs[]`, `attaches_to[]`, `operates_on[]`, `participants[]`, `triggers[]`, and `extends[]` must resolve to an existing card in (tenant ∪ pack). This is checked by `gate_refs_resolve(card_id, refs, resolver_set)` from `ontology_foundry/eval/gates.py`.

### 5.2 Kind constraints

| Field | Expected ref kind |
|---|---|
| `extends` | `interface` |
| `refs` | `object_type` (default) — kind hints permitted, see §5.4 |
| `subject_refs` (causal_node) | `object_type` |
| `outcome_refs` (causal_node) | `object_type` |
| `attaches_to` (derived_state) | `object_type` |
| `operates_on` (action) | `object_type` |
| `participants` (event) | `object_type` |
| `triggers` (event) | `action` |

When a ref is to a non-default kind, qualify it: `refs: [employee, derived_state:overdue_assignment]`. Unqualified refs default to `object_type`.

### 5.3 Cycles

Cycles are not inherently forbidden for `object_type` refs (Employee → Manager → Employee is legitimate self-reference). Cycles in **`extends`** chains are forbidden; cycles in causal_node `subject_refs` → outcome chains imply feedback loops, which the foundry's `directed_graph_has_cycle` check from `eval/causal_checks.py` flags for review but does not auto-reject.

### 5.4 Inbound discovery

A card does not list its inbound refs; those are derived. The foundry materializes an inverse-ref index in Postgres (§9.2). Card authors only declare what *this* card refers to, never what refers to it.

---

## 6. Markings

### 6.1 Vocabulary (seed)

| Marking | Meaning |
|---|---|
| `contains_pii` | Card represents PII or has fields that are PII. |
| `contains_phi` | Protected Health Information (HIPAA). |
| `contains_pci` | Payment card data (PCI-DSS). |
| `regulated_<regime>` | E.g., `regulated_hipaa`, `regulated_sox`, `regulated_gdpr`. |
| `confidential` | Org-internal sensitivity. |
| `restricted` | Higher than confidential; access-controlled. |
| `audited_writes` | Mutations on bindings of this card must be logged. |

Vocabulary is extensible per org via a `markings_vocab.yaml` in the tenant root.

### 6.2 Propagation

Markings propagate **downward** through bindings to MDL fields:

- An `object_type` card marked `contains_pii` propagates `contains_pii` to every MDL field bound to its identity field.
- An `interface` card's markings propagate to all `object_type`s that `extends` it.
- A `causal_node` card's markings do **not** propagate to subjects/outcomes — causal participation isn't a propagating signal.

Propagation is computed by the bindings layer (see `mdl_bundle_spec.md`), not stored redundantly in MDL files. Authors mark the card; the system stamps the bindings.

### 6.3 No automatic enforcement at this layer

Per the T0/T1 lock (`T0_T1_organization_source_spec.md` §4), markings are **declarative**. They drive governance configuration and best-effort checks downstream; they are not enforced at the card storage layer.

---

## 7. Versioning

### 7.1 Per-card version

`version` is a positive integer in frontmatter. Bumped on **every save** that changes any field (frontmatter or body). Bump rule: `version_new = version_old + 1`. No semantic versioning; integer monotone.

### 7.2 Version retention

The Postgres `card` table retains all versions (append-only on bump). The filesystem stores the current version only; prior versions are reconstructable from Postgres + git history.

### 7.3 Consumers reference current version by default

Bindings and downstream cards refer to cards by `id`, not `(id, version)`. They resolve to the current version at read time. Consumers that need pinned versions (rare; mainly evaluation harnesses) may use `(id, version)` tuples — supported by the Postgres index, not the filesystem.

### 7.4 Audit

Each version bump writes an audit row: `(card_id, kind, old_version, new_version, actor, changed_fields[], occurred_at)`. Audit is the same `hierarchy_audit` table introduced in `T0_T1_organization_source_spec.md` §5 (forward-deferred); cards reuse it with `tier = 'semantic_layer'`.

---

## 8. Pack vs tenant origin

| `origin` | Mutability | Storage |
|---|---|---|
| `pack` | Read-only at runtime | Shipped in pack bundles; mounted read-only into tenant view |
| `tenant` | Mutable; authored locally | Filesystem under `tenants/<org_id>/semantic_layer/` |
| `derived` | Generated by foundry pipelines; refresh-on-rerun | Filesystem under `tenants/<org_id>/semantic_layer/_derived/` |

`derived` cards are produced by the foundry's extraction passes (e.g., a `causal_node` proposed from observed correlations + LLM hypothesis). They land as drafts; humans review and promote to `tenant` origin (moving the file out of `_derived/` and bumping `version`).

---

## 9. Storage model

Three-store mirror:

### 9.1 Filesystem (source of truth)

Authoritative for content. Git-versioned. Diff-reviewable. The filesystem path encodes (`tenant`, `kind`, `id`); the frontmatter must agree.

### 9.2 Postgres `card` table (index + traversal)

```sql
CREATE TABLE card (
  tenant_id        text NOT NULL,
  layer            text NOT NULL,
  kind             text NOT NULL,
  id               text NOT NULL,
  version          integer NOT NULL,
  origin           text NOT NULL,  -- 'pack' | 'tenant' | 'derived'
  frontmatter      jsonb NOT NULL, -- parsed YAML
  body             text NOT NULL,
  body_hash        text NOT NULL,  -- sha256 of body, for diff detection
  embedding_id     text,           -- Qdrant point id (= rk)
  valid_from       timestamptz NOT NULL DEFAULT now(),
  valid_to         timestamptz,    -- NULL for current
  deprecated       boolean NOT NULL DEFAULT false,
  superseded_by    text,
  PRIMARY KEY (tenant_id, layer, kind, id, version)
);

CREATE INDEX idx_card_current ON card (tenant_id, layer, kind, id)
  WHERE valid_to IS NULL;

CREATE TABLE card_ref (
  tenant_id        text NOT NULL,
  from_kind        text NOT NULL,
  from_id          text NOT NULL,
  to_kind          text NOT NULL,
  to_id            text NOT NULL,
  ref_field        text NOT NULL,  -- 'refs' | 'extends' | 'subject_refs' | ...
  PRIMARY KEY (tenant_id, from_kind, from_id, to_kind, to_id, ref_field)
);

CREATE INDEX idx_card_ref_inbound ON card_ref (tenant_id, to_kind, to_id);
```

`card_ref` is the inverse-ref index. It enables "what refers to this card" without scanning all cards.

### 9.3 Qdrant `cards` collection (semantic search)

| Property | Value |
|---|---|
| Collection | `cards_<tenant_id>` (one per tenant) |
| Vector | Embedding of body text (concatenated with frontmatter `aliases` if present) |
| Payload | `{ layer, kind, id, version, origin, markings[], refs[], extends[], deprecated }` |
| Point id | `{tenant_id}::{layer}::{kind}::{id}` (current version) |

Re-embedded on body change (detected via `body_hash` mismatch). Payload-only fields (e.g., `deprecated` flip) update payload without re-embedding.

Embedding model matches whatever the existing causal-ontology card_emitter uses (see `card_emitter_design.md` — single model across the foundry).

---

## 10. Validation gates

All gates live in `ontology_foundry/eval/`. They are invoked by:
- The CI authoring pipeline (block on FAIL for `tenant` cards).
- The foundry's promotion step from `_derived/` → `tenant/`.
- The card-store write API.

### 10.1 Per-card gates

| Gate | From `eval/gates.py` | Behavior |
|---|---|---|
| Non-empty body | `gate_nonempty_body(body, min_chars=1)` | Body must contain at least one non-whitespace char. Existing implementation. |
| ID pattern | `gate_id_pattern(card_id, pattern=r"^[a-z][a-z0-9_]*$")` | Existing implementation. |
| Refs resolve | `gate_refs_resolve(card_id, refs, resolver_set)` | Every entry in any ref-shaped field resolves. Existing implementation. **Pass `resolver_set = card_ref_universe(tenant_id) ∪ pack_universe()`.** |
| File↔frontmatter agree | new | Parent directory matches `kind`; filename matches `id`. |
| Required fields per kind | new | E.g., `causal_node` requires `subject_refs` and `outcome_refs` non-empty. |
| Markings vocabulary | new | Every entry in `markings[]` is in the tenant's markings vocab. |
| Cycle in `extends` | new | DFS over interface extension chain. |
| Pack-overlay legality | new | A tenant card with `origin: pack` is rejected. |

New gates are added as named functions in `ontology_foundry/eval/gates.py` alongside the existing three. Same `(GateVerdict, list[EvalIssue])` return shape.

### 10.2 Cross-card gates (corpus-level)

Run by the CI authoring pipeline against the full tenant card set:

| Gate | Behavior |
|---|---|
| All inbound refs satisfied | For every card, its outbound refs resolve. |
| No `extends` cycles | Topological sort succeeds on interfaces. |
| No duplicate `id` within `(layer, kind)` | Unique constraint check. |
| Deprecation chain terminates | `superseded_by` chains end at a non-deprecated card. |
| Causal DAG check | Optional. `directed_graph_has_cycle` from `eval/causal_checks.py` against the `(causal_node, subject_refs, outcome_refs)` graph. Cycles flagged for review, not auto-rejected (per §5.3). |

### 10.3 Regression gates

Reuses `regression_gate_quality` and `regression_gate_zero_tolerance` from `eval/regression.py`. Cards fail the gate if the change regresses retrieval P/R or grounding scores by more than the configured threshold against a held-out card-aware eval set.

---

## 11. Authoring workflow

### 11.1 Human authoring (tenant cards)

1. Edit `tenants/<org_id>/semantic_layer/<kind>s/<id>.card.md` in an editor or PR.
2. CI runs the per-card gates (§10.1) on changed files.
3. CI runs cross-card gates (§10.2) on the full tenant set.
4. On gate pass, merge writes:
   - Bump `version` in frontmatter (CI may do this automatically or require explicit bump).
   - Write a new row in Postgres `card` table; mark prior row's `valid_to`.
   - Rebuild `card_ref` rows for the changed card.
   - Re-embed in Qdrant if body changed.
   - Write audit row.

### 11.2 LLM-assisted authoring (derived cards)

1. Foundry extraction pipeline (e.g., `OntologyFoundryPipeline` over docs + tabular bundles) proposes a card.
2. Proposed card lands at `tenants/<org_id>/semantic_layer/_derived/<kind>s/<id>.card.md` with `origin: derived`.
3. Human review opens a PR moving the file out of `_derived/`, changing `origin: tenant`, and possibly editing.
4. Standard authoring workflow (§11.1) applies on the PR.

### 11.3 Bulk import (pack onboarding)

Pack cards arrive as a versioned directory tree. Onboarding writes pack-origin rows to the `card` table but does **not** write files into the tenant tree. Pack updates are version-pinned per tenant (a tenant pins which pack version it consumes).

---

## 12. Relationship to MDL and bindings

### 12.1 The card prose names the binding

The card body for `employee` says "Employees are sourced from the CSOD employee table. The employee_id field maps to EmployeeID." This sentence is the **human-readable** form of the binding.

### 12.2 Machine-readable form lives in `mdl_bundle_spec`

Per asset, `semantic_bindings.json` (defined in `mdl_bundle_spec.md`) carries the structured form. The bindings file references cards by `id`, not by version; resolution uses the current version per §7.3.

### 12.3 Card edits trigger bindings re-extraction

When a card's body changes (detected via `body_hash`), the foundry enqueues bindings re-extraction for every asset that currently references the card. The LLM re-parses the card body and proposes any binding deltas; conflicts (e.g., the card now says "EmployeeNumber" not "EmployeeID") surface as draft binding updates for human review.

### 12.4 Cards do not embed MDL knowledge

The card body should **not** restate column types, table cardinalities, or partition strategies — those live in MDL and are vendor-extracted. The card describes the *concept*; the MDL describes the *physics*. The bridge connects them.

---

## 13. Examples

### 13.1 Object type

```markdown
---
id: employee
layer: semantic
kind: object_type
version: 3
extends: [trainable, auditable]
markings: [contains_pii]
refs: [department, role, manager, training_assignment]
---
An Employee is a person who works at the organization. Each employee is
identified by an employee_id, which is PII and propagates that marking to any
derived field.

Employees belong to exactly one department, report to one manager (who is
themselves an employee), and hold one role. Their employment_status is one of
active, on leave, or terminated. Only active employees can be assigned new
training — this is enforced as a precondition on the AssignTraining action.

Employees are sourced from the CSOD employee table. The employee_id field maps
to EmployeeID; department_id and manager_id resolve to other Employee and
Department cards via foreign keys.

Employees implement the Trainable interface (because they can receive training
assignments) and the Auditable interface (every action on them is logged). They
participate in causal reasoning through OverdueRisk, ComplianceGap, and
PhishingRisk causal nodes whenever those nodes need a per-employee scope.
```

### 13.2 Interface

```markdown
---
id: trainable
layer: semantic
kind: interface
version: 1
contributes_refs: [training_assignment, course]
---
A Trainable object is one that can receive training assignments. Implementers
gain a many-to-many relationship to Course through TrainingAssignment, and are
expected to expose an employment_status (or analogous activity status) that
governs whether new assignments may be created against them.

Trainable does not itself carry markings. Implementers carry their own.
```

### 13.3 Causal node

```markdown
---
id: overdue_risk
layer: semantic
kind: causal_node
version: 2
subject_refs: [employee, training_assignment]
outcome_refs: [compliance_gap]
markings: []
---
OverdueRisk is the per-employee risk that one or more required training
assignments will become overdue within a forecast horizon. It is computed from
the set of pending and in_progress TrainingAssignments for an Employee, their
due_dates, and the Employee's historical late-completion rate.

When OverdueRisk exceeds the configured threshold (default 0.6), the Employee
becomes a contributor to the Department-level ComplianceGap causal node, which
in turn drives the rollup risk surfaced in the compliance dashboard.

Mechanism: late_completion rate (priors) × open_assignment_count near due_date
× employment_status. The mechanism is described qualitatively here; the
quantitative refit lives in the causal pipeline (PC + LiNGAM consensus over
the org training scenario).
```

### 13.4 Derived state

```markdown
---
id: overdue_assignment
layer: semantic
kind: derived_state
version: 1
attaches_to: [training_assignment]
condition_summary: due_date < now() AND completed_date IS NULL
---
An OverdueAssignment is a TrainingAssignment whose due_date has passed and
whose completed_date is still null. The state attaches at the moment the
due_date is crossed and detaches if the assignment is completed (transitioning
instead to LateCompletion).

OverdueAssignment is the primary signal feeding the OverdueRisk causal node
for the assigned Employee, and it triggers escalation actions per the
configured compliance policy.
```

### 13.5 Action

```markdown
---
id: assign_training
layer: semantic
kind: action
version: 1
operates_on: [employee, course]
preconditions_summary: employee.employment_status = 'active'
effects_summary: creates a TrainingAssignment with status='pending'
---
AssignTraining creates a new TrainingAssignment linking an Employee to a
Course with a specified due_date.

Precondition: the target Employee's employment_status must be 'active'.
Assignments to employees on_leave or terminated are rejected at the
application layer; the audit log records rejected attempts.

Effect: a new TrainingAssignment row with assigned_date = now(),
due_date = <input>, completed_date = null, status = 'pending'. The newly
created assignment becomes part of the Employee's open-assignment set and
contributes to OverdueRisk recompute on the next scheduled pass.
```

---

## 14. Operations contract (foundry-side)

```python
# ontology_foundry/semantic_layer/card_store.py (to be added)

class CardStore(Protocol):
    def get(self, *, tenant_id: str, kind: str, id: str,
            version: int | None = None) -> Card: ...
    def list_by_kind(self, *, tenant_id: str, kind: str,
                     include_deprecated: bool = False) -> list[Card]: ...
    def list_refs(self, *, tenant_id: str, kind: str, id: str,
                  direction: Literal["out", "in"] = "out") -> list[CardRef]: ...
    def search(self, query: str, *, tenant_id: str, kind: str | None = None,
               markings: list[str] | None = None,
               k: int = 10) -> list[CardHit]: ...
    def upsert(self, card: Card, *, actor: str) -> CardWriteResult: ...
    def deprecate(self, *, tenant_id: str, kind: str, id: str,
                  superseded_by: str | None = None, actor: str) -> None: ...
    def validate(self, card: Card, *, tenant_id: str) -> list[EvalIssue]: ...
    def resolver_set(self, *, tenant_id: str) -> set[tuple[str, str]]: ...  # (kind, id) pairs
```

`validate` runs the gates from §10.1 and returns the `EvalIssue` list (the same shape `ontology_foundry/eval/models.EvalIssue` uses).

`upsert` is the write path: it validates, writes Postgres, queues Qdrant re-embedding, writes the audit row.

`resolver_set` is the helper for `gate_refs_resolve`: the set of `(kind, id)` pairs in scope (tenant ∪ pack).

---

## 15. Open items (deferred)

- **Multilingual cards.** If T0 declares multiple `supported_languages`, the system may want translations of card bodies. Out of scope here; revisit when first multilingual tenant onboards.
- **Card embeddings per-section.** Currently one embedding per card body. A future refinement is per-paragraph embeddings for finer-grained retrieval. Defer; measure first via the eval harness.
- **Card-level access control.** Markings imply access constraints; the access-control enforcement lives in the governance profile (forward-referenced from T0). Spec'd separately.
- **Card diff visualization in PRs.** Useful for human review of LLM-assisted edits. Tooling concern, not specced here.

---

## 16. Change log

| Date | Change |
|---|---|
| 2026-05-15 | Initial draft. |
