# Configuration and Source Strategy

How the system is configured across platform, domain pack, and tenant layers,
how source materials are organized in local storage, and how the extraction
pipeline handles the wide range of source types — from full warehouse data
to API-only metadata where sample data and profiling statistics may be
entirely absent.

---

## 1. Why This Document Exists

The extraction pipeline was originally specified assuming full data access:
warehouses to profile, samples to correlate, outcome streams to learn from.
Real customer environments are rarely that uniform. A typical deployment
mixes:

- **Warehouse-resident data** (CSOD exports in Snowflake, training events in
  Databricks) — full profiling and correlation possible.
- **API-accessible systems** (Salesforce, Workday, ServiceNow, Cornerstone,
  Veeva Vault) — metadata is rich, but sample data is restricted by rate
  limits, permissions, or policy.
- **Document-only sources** (compliance policies, regulatory frameworks,
  training materials in PDF/Word/Markdown) — no schema, no data, just prose.
- **Hybrid sources** where some objects are accessible at the data level and
  others are metadata-only.

The pipeline must handle this heterogeneity gracefully. Operators that
require sample data (correlators, causal discoverers) should degrade cleanly
when data is unavailable — not fail, not silently produce empty findings.
Cards generated from metadata-only sources should carry appropriately
calibrated confidence so downstream causal queries don't treat them as if
they were learned from outcome data.

This document specifies:

- The three-tier configuration model (platform / pack / tenant) that drives
  the system.
- The local storage convention for documents and data, with lifecycle stages.
- The taxonomy of source types and how each affects pipeline behavior.
- API and metadata-only source handling — what's possible, what isn't, and
  how cards built from them are calibrated.
- The operator behavior matrix: which operators run in which source modes.
- Phased delivery for configuration support.

---

## 2. Three-Tier Configuration Model

Configuration sits at three levels. Each layer specializes the one above
without overriding it implicitly.

```
┌────────────────────────────────────────────────────────────────┐
│  PLATFORM CONFIG                                                │
│  Framework defaults, model registry, global thresholds          │
│  Owned by: platform team                                        │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼  inherits and overlays
┌────────────────────────────────────────────────────────────────┐
│  DOMAIN PACK CONFIG                                             │
│  Seed concepts, CDM references, NER types, causal priors,       │
│  rule templates, golden datasets                                │
│  Owned by: domain team (LMS+security, eClinical, etc.)          │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼  inherits and overlays
┌────────────────────────────────────────────────────────────────┐
│  TENANT CONFIG                                                  │
│  Source paths, schema mapping, schedules, tenant overrides,     │
│  permissions, secrets references                                │
│  Owned by: deployment team / customer                           │
└────────────────────────────────────────────────────────────────┘
```

### 2.1 Platform config

The platform layer owns framework-wide defaults. Models, embedders, default
similarity thresholds, retry policies, observability targets, secret
backend choice. This rarely changes; updates are platform releases.

```yaml
# platform.config.yaml

framework_version: 1.4.0

models:
  default_embedder: bge-large-en-v1.5
  fallback_embedder: text-embedding-3-large
  ner_pipeline_default:
    - spacy_en_web_trf
    - gliner_domain
    - causal_marker_rules
  
  llm_routing_defaults:
    claim_extractor_strong: claude-opus-4-7
    claim_extractor_routine: claude-sonnet-4-6
    summarizer: claude-haiku-4-5
    judge: claude-sonnet-4-6

similarity_thresholds:
  object_type_link: 0.92
  concept_link: 0.78
  causal_marker: 1.0  # rule-based, exact match

retry_policies:
  llm_call:
    max_attempts: 3
    backoff: exponential
  api_fetch:
    max_attempts: 5
    backoff: exponential_with_jitter

observability:
  trace_provider: openlineage
  llm_trace: langfuse
  metrics: prometheus

secrets:
  backend: vault  # or aws_secrets_manager, databricks_secrets, kubernetes
```

### 2.2 Domain pack config

A domain pack is a versioned bundle of domain knowledge. It carries seed
concepts, CDM references, NER types, causal priors, rule templates, and
golden datasets specific to a domain. Packs are first-class versioned
artifacts; tenants instantiate them.

```yaml
# domain_packs/lms_security/v2.3/pack.config.yaml

pack_id: lms_security
pack_version: 2.3.0
description: LMS compliance and security risk reasoning

# Seed concepts ship as pre-built cards
seed_concepts_dir: ./seed_concepts/
seed_object_types_dir: ./seed_object_types/
seed_link_types_dir: ./seed_link_types/

# CDM references — canonical entity definitions
cdm_references:
  - kind: nist_csf
    version: 2.0
    path: ./cdm_references/nist_csf_2.0.yaml
    anchors_to:
      - control: cdm.security.control
      - function: cdm.security.function
  
  - kind: mitre_attack
    version: v15
    path: ./cdm_references/mitre_attack_v15.yaml
    anchors_to:
      - tactic: cdm.security.tactic
      - technique: cdm.security.technique
  
  - kind: cwe
    version: 4.13
    path: ./cdm_references/cwe_4.13.yaml
    anchors_to:
      - weakness: cdm.security.weakness

# NER type set — beyond platform defaults
ner_type_set:
  - policy_clause
  - compliance_framework
  - control_id
  - cwe_reference
  - cve_reference
  - training_event
  - learning_objective

# Causal priors — hypothesized edges with literature backing
causal_priors_dir: ./causal_priors/

# Derivation and validation rule templates
rule_templates_dir: ./rule_templates/

# Ontology imports
ontology_imports:
  - cwe
  - mitre_attack
  - nist_csf

# Golden datasets for evals
golden_datasets:
  card_corpus: ./golden/cards.jsonl
  knowql_queries: ./golden/queries.jsonl
  causal_claims: ./golden/causal.jsonl
  hallucination_probes: ./golden/probes.jsonl

# Pack supports these source types
supported_source_types:
  - warehouse_full
  - api_with_data
  - api_metadata_only
  - documents_only
  - hybrid

# Preferred CDM mapping for common LMS tables
default_schema_mapping_hints:
  "*.employee": cdm.identity.user
  "*.training_assignment": cdm.training.assignment
  "*.course": cdm.training.learning_object
  "*.role": cdm.identity.role
```

A second pack, `eclinical_v1.0`, would have an analogous shape with
`omop_cdm`, `cdisc_sdtm`, `meddra`, `snomed_ct`, `rxnorm` as references and
a different NER type set (`medication_name`, `dose`, `meddra_term`, etc.).

### 2.3 Tenant config

The tenant layer is short and declarative. It picks a pack, points at
sources, declares schema mappings, and sets schedules.

```yaml
# tenants/acme_corp/tenant.config.yaml

tenant_id: acme_corp
active_pack: lms_security
active_pack_version: 2.3.0
pack_overrides_dir: ./overrides/

# Sources — see §3 for full layout
sources:
  documents:
    base_path: /workspace/data/acme_corp/documents
    enabled: true
  
  schemas:
    base_path: /workspace/data/acme_corp/schemas
    enabled: true
    sources:
      - kind: dbt_manifest
        path: ./csod_dbt/manifest.json
      - kind: ddl_dump
        path: ./csod_schema.sql
        dialect: postgresql
  
  data_samples:
    base_path: /workspace/data/acme_corp/data
    enabled: true
    profile_full: false
    sample_size: 100000
  
  api_sources:
    - kind: salesforce
      mode: metadata_with_sample
      credentials_secret: ${secret:acme_sf_creds}
      sample_query_limits:
        max_rows_per_object: 1000
        rate_limit_per_minute: 60
    
    - kind: cornerstone_ondemand
      mode: metadata_only
      credentials_secret: ${secret:acme_csod_creds}
    
    - kind: servicenow
      mode: api_with_data
      credentials_secret: ${secret:acme_snow_creds}
  
  outcomes:
    base_path: /workspace/data/acme_corp/outcomes
    enabled: false  # not yet wired up

# Schema mapping — declared, not inferred
schema_mapping:
  csod.employee: cdm.identity.user
  csod.training_assignment: cdm.training.assignment
  csod.course: cdm.training.learning_object
  salesforce.Contact: cdm.identity.external_user
  servicenow.incident: cdm.security.incident

# Execution
execution:
  default_backend: standalone
  backend_overrides: {}

storage:
  card_store: postgres
  vector_store: qdrant
  artifact_store: ./workspace/artifacts/acme_corp

models:
  ner_pipeline: default  # use platform default
  llm_routing: default

pipeline_schedule:
  ingestion: "0 3 * * *"
  weight_learning: "0 4 * * 0"

permissions:
  default_role: compliance_analyst
  pii_clearance_roles: [hr_admin, security_lead]
```

### 2.4 Configuration resolution

At pipeline startup, configurations are loaded and merged in this order:

1. Load platform defaults.
2. Load active domain pack — overlays platform defaults.
3. Load tenant config — overlays pack values.
4. Apply tenant `pack_overrides_dir` content — replaces specific pack-shipped
   cards or rules with tenant-customized versions.
5. Resolve secrets references against the configured secret backend.
6. Validate the merged config against a Pydantic schema; fail fast on
   conflicts.

Resolution is **explicit, not implicit**. Tenants override by declaration,
not by silent shadowing. When a tenant overrides a pack-shipped concept
card, the audit log records which card was overridden, when, and why.

### 2.5 Pack upgrades

When a pack ships a new version (e.g., `lms_security_v2.4`), tenants on the
prior version see an upgrade-available signal. Upgrade is a card-level merge:

- Pack-shipped cards that have not been tenant-overridden: get the new
  version automatically.
- Pack-shipped cards that have been tenant-overridden: stay at the tenant
  version, with a notification that upstream changed.
- Tenant-only cards: unaffected.
- Removed pack cards: deprecated in the tenant ontology with a 90-day
  grace period.

The upgrade process generates a diff report. Reviewers approve the merge
before it commits.

---

## 3. Local Storage Convention

For document construction and source artifact handling, sources live locally
under a known structure. Source adapters discover content by convention,
which keeps tenant onboarding to "drop the right files in the right
directories."

### 3.1 Tenant directory layout

```
/workspace/data/<tenant>/
  documents/
    inbox/                    ← landing zone for new files (pre-staging)
    staging/                  ← validated, classified, ready for ingestion
      policies/
      training_materials/
      compliance/
      runbooks/
      architecture/
      regulatory/
    processed/                ← already ingested (kept for retention)
    archive/                  ← cold storage after retention period
  
  schemas/
    dbt_manifests/
    ddl_dumps/
    catalog_exports/
  
  data/
    samples/                  ← tabular samples for profiling
    profiles/                 ← computed profiling artifacts
  
  api_pulls/
    metadata/                 ← cached API metadata (sObjects, fields, etc.)
    samples/                  ← sampled API data when permitted
    schedules/                ← API pull schedules and watermarks
  
  cdm_overrides/              ← tenant-specific CDM extensions
  
  outcomes/
    incoming/                 ← raw outcome events
    processed/                ← labeled outcome batches
  
  workspace/
    artifacts/                ← intermediate artifacts during runs
    logs/
    snapshots/                ← graph snapshots from maintainer
```

Pack defines canonical subdirectories. Tenant config can extend them.
Adapters read configured paths, not hardcoded ones.

### 3.2 Document lifecycle

Documents move through four lifecycle stages in local storage:

```
inbox/        ← any file format, no metadata required
  ↓ classification + metadata generation
staging/      ← validated, classified, _metadata.yaml attached, ready
  ↓ pipeline ingestion
processed/    ← content_hash recorded as ingested, retained for retention period
  ↓ retention expiry
archive/      ← cold storage, retrievable but not ingested
```

A small file watcher (or scheduled job) handles inbox-to-staging transition:
detects new files, runs classification, generates `_metadata.yaml`, and moves
the file. The pipeline reads only from staging. After ingestion, files move
to processed; after retention period, to archive.

This keeps the pipeline idempotent — re-runs see only staging — and
provides clear forensics for "where is this document right now."

### 3.3 Document metadata

Each document directory under staging carries a `_metadata.yaml`:

```yaml
# staging/policies/_metadata.yaml

documents:
  - filename: training_policy_2026.pdf
    intake_date: 2026-04-15
    source_authority: legal_team
    classification: internal
    retention_days: 365
    document_type: compliance_policy
    confidence_in_source: high
    notes: "FY2026 policy update, supersedes 2025 version"
  
  - filename: phishing_guidelines.md
    intake_date: 2026-04-20
    source_authority: security_team
    classification: internal
    retention_days: 365
    document_type: training_guideline
    confidence_in_source: high
```

The pipeline reads this for provenance and marking propagation. A document
under `policies/` with `authority: legal_team` and `classification: internal`
inherits stronger marking than one under `training_materials/` with
`authority: hr` and `classification: public`.

`confidence_in_source` is a tenant-declared signal that flows into card
generation. A vendor whitepaper carries `medium`; an internal policy
carries `high`; a forum thread carries `low`. This ranks claims when
multiple sources discuss the same topic.

### 3.4 Schema and data layout

```
schemas/
  dbt_manifests/
    csod_dbt/
      manifest.json
      catalog.json
  
  ddl_dumps/
    csod_schema.sql
    workday_extract.sql
  
  catalog_exports/
    unity_catalog_export.json
    atlan_export.json

data/
  samples/
    csod.employee.parquet
    csod.training_assignment.parquet
  
  profiles/
    2026-04-15/
      csod.employee.profile.json
      csod.training_assignment.profile.json
```

Schemas and data are independently optional. A tenant might have `schemas/`
populated but `data/samples/` empty — that's the metadata-only mode covered
in §5.

---

## 4. Source-Type Taxonomy

The pipeline must handle four source modes, each with different data
availability and therefore different operator behavior.

| Mode                     | Schema     | Sample data | Statistics  | Outcome data | Documents  |
| ------------------------ | ---------- | ----------- | ----------- | ------------ | ---------- |
| **Mode A: Warehouse Full** | Yes        | Yes (full or sampled) | Yes      | Often yes    | Maybe      |
| **Mode B: API With Data**  | Yes        | Yes (limited via API) | Approximate | Sometimes  | Sometimes  |
| **Mode C: Metadata Only**  | Yes        | No          | No          | No           | Sometimes  |
| **Mode D: Documents Only** | No         | No          | No          | No           | Yes        |

Real deployments mix these. A tenant might have CSOD in Mode A, Salesforce
in Mode B, Cornerstone in Mode C, and a folder of policy PDFs in Mode D —
all feeding the same ontology.

### 4.1 Mode A: Warehouse Full

Customer's data lives in Snowflake, Databricks, BigQuery, or similar.
Schemas accessible via DDL or dbt manifest. Sample data accessible by query.
Often outcome data also in the warehouse.

This is the original assumption of the extraction pipeline. Every operator
runs as designed: profilers compute exact statistics, correlators have
populated tables to work with, causal discovery has data, the Weight
Learner has labeled outcomes.

The Spark backend (§7 of extraction design) shines here — co-located with
warehouse data.

### 4.2 Mode B: API With Data

Customer's primary system is an API-driven SaaS (Salesforce, Workday,
ServiceNow, Cornerstone OnDemand, Veeva Vault). Schema is rich and
declarative. Sample data accessible via the API, but with constraints:

- Rate limits cap how much can be pulled per hour or day.
- Permissions may restrict access to certain objects or fields.
- Some objects are queryable, others are only metadata-readable.
- Cross-object queries are limited.
- Historical data may be accessible, but full-table scans are usually not.

The pipeline pulls metadata fully (it's small and rate-friendly) and
samples data conservatively (within API limits, with explicit per-object
quotas in tenant config).

Profilers run on samples but mark statistics as **approximate** with the
sample size recorded. Correlators run on samples with explicit caveats.
Causal discovery is feasible on sufficient samples but emits
`weight.source: hypothesized_from_sample` rather than `learned`.

### 4.3 Mode C: Metadata Only

The case the user flagged: rich metadata, but **no sample data, no
profiling stats, often no outcome data**. Could be:

- API access permits metadata reads but not data queries (security or
  privacy policy).
- API rate limits make data sampling impractical.
- The system is documented but not directly accessible (legacy system
  described in PDFs).
- Customer policy explicitly forbids data egress.

This is where the pipeline must degrade most. **Operators that depend on
data fail or skip cleanly; operators that work from metadata produce
useful but appropriately calibrated cards.**

What's still possible from metadata + documents alone:

- `object_type` cards from API metadata (object names, descriptions, fields)
- `property_type` cards from field metadata (types, picklists, validation
  rules, formula expressions)
- `link_type` cards from declared relationships (lookup, master-detail,
  reference fields)
- `concept` cards from documentation and CDM seed
- `causal_node` cards from CDM seed and pack-shipped causal priors
- `causal_edge` cards as `weight.source: literature` or `pack_default`,
  not learned
- `derivation_rule` cards from validation rules and formula fields in
  metadata
- `validation_rule` cards from API constraint declarations
- `marking` and `permission` cards from API permission models

What is **not** possible without data:

- Statistical correlations (no data to correlate)
- Algorithmic causal discovery (PC, FGES, etc. need data)
- Weight learning (no outcomes to learn from)
- Distribution-based property semantics (range, top-k values)
- Empirical FK validity (referential integrity rates)

### 4.4 Mode D: Documents Only

Weakest mode. No schema, no data, no API. Just prose documents — policies,
runbooks, regulatory filings, training materials.

What's possible: `concept` cards from claim extraction, soft `link_type`
cards from co-occurrence, `derivation_rule` and `validation_rule` cards
from policy text, governance cards from access policy documents.

What's not: anything that needs a structural anchor (object_type,
property_type, structural link_type) — these depend on schema. The pack's
seed object types provide a fallback anchor when documents reference
generic entities, but the resulting cards are coarse.

---

## 5. API and Metadata-Only Source Handling

The pipeline section that requires the most careful design, because
metadata-rich-but-data-poor is both the most common API case and the most
likely to silently produce low-quality cards if not handled deliberately.

### 5.1 What API metadata gives you

Even without data, API metadata is rich. For Salesforce specifically:

| Metadata kind               | Information                                              |
| --------------------------- | -------------------------------------------------------- |
| sObject definitions          | Object name, label, description, custom vs. standard     |
| Field definitions            | Type, label, description, length, precision              |
| Field-level metadata         | Required, unique, defaultedOnCreate, autonumber           |
| Picklist values              | Allowed values with labels — strong type signal           |
| Formula fields               | Expression text — reveals intended derivations            |
| Validation rules             | Rule expressions — reveals business constraints           |
| Workflow rules               | Triggers and actions — reveals process logic              |
| Relationships                | Lookup, master-detail, hierarchical                       |
| Record types                 | Subclassification of objects                              |
| Profiles and permission sets | Field-level and object-level access control               |
| Sharing rules                | Multi-tenant access patterns                              |
| Triggers (Apex)              | Custom logic at the data layer                            |

Most other enterprise APIs (Workday, ServiceNow, Cornerstone, Veeva, Jira,
NetSuite) expose analogous metadata. The shapes differ; the richness is
similar.

### 5.2 What this means for the pipeline

Metadata is a substitute for data in some cases and not in others. The
pipeline distinguishes these explicitly.

**Metadata can substitute for data:**

- *Type information.* A `picklist` field with 5 values is a categorical
  variable; the value enum is known without sampling.
- *Structural relationships.* A `lookup` or `master-detail` field declares
  a foreign key explicitly. No FK inference needed.
- *Validation logic.* A validation rule expression reveals what the
  business considers a valid record. The pipeline emits a `validation_rule`
  card from the rule expression directly.
- *Derivation logic.* A formula field expression reveals an intended
  derivation. The pipeline emits a `derivation_rule` card with the formula
  as its body.
- *Governance.* Profile and permission set metadata maps directly to
  `role`, `permission`, and `marking` cards.
- *Causal hints.* Workflow rules of the form "when status changes to X,
  set Y to Z" are deterministic causal claims about the system itself.

**Metadata cannot substitute for data:**

- *Distribution.* No way to know null rate, value distribution, or
  cardinality from metadata alone.
- *Empirical correlations.* If two fields tend to vary together, only
  data reveals that.
- *Real causal effects.* Hypothesized edges from CDM and literature are
  not learned weights.
- *Outcome-driven calibration.* Without outcomes, weights stay at their
  prior values forever.

### 5.3 The metadata adapter

Every API source has a metadata adapter that emits typed artifacts from
metadata pulls. The adapter's contract:

```python
class ApiMetadataAdapter:
    def fetch_metadata(self) -> ApiMetadataArtifact:
        """Pull object/field/relationship metadata from the API.
        Always permitted, rate-friendly."""
    
    def fetch_sample(self, object_name: str, limit: int) -> Optional[DataArtifact]:
        """Pull sample data for one object, respecting limits.
        May return None if not permitted or rate-limited."""
    
    def supports_data_access(self) -> bool:
        """Whether this tenant configuration permits sample data."""
```

The pipeline calls `fetch_metadata` always and `fetch_sample` only if
permitted by tenant config and within rate budgets.

Adapters ship with the platform for common APIs. Customers can write their
own adapters for proprietary systems by implementing the same contract.

### 5.4 Cards from metadata

Concrete mapping from API metadata to card kinds, using Salesforce as the
example:

| Metadata source                    | Becomes card kind             | Notes                                       |
| ---------------------------------- | ----------------------------- | ------------------------------------------- |
| sObject (Account, Contact, etc.)   | `object_type`                  | Description from API maps to card body      |
| Field (custom or standard)          | `property_type`                | Type, label, description, picklist values   |
| Picklist values                     | `concept` per value            | Values with descriptions become small concept cards |
| Lookup / master-detail relationship | `link_type`, derivation: structural | Direct from metadata, no inference  |
| Formula field                       | `derivation_rule`              | Formula expression in card body              |
| Validation rule                     | `validation_rule`              | Rule expression in card body                 |
| Workflow rule                       | `causal_rule`                  | Trigger-action pairs as deterministic causality |
| Apex trigger                        | `function`, kind: code         | Code body referenced; behavior described     |
| Profile                             | `role`                         | Permission set membership                    |
| Permission set                      | `role`                         | Bundles of permissions                        |
| Field-level security                | `permission` + `marking`       | Restricted fields get markings                |
| Sharing rule                        | `permission`                   | Conditional access patterns                  |
| Record type                         | `object_type` subtype          | Subclassification card                       |

Workflow rules and triggers as `causal_rule` cards is the design choice
worth highlighting. A workflow rule that says "when Opportunity.Stage
changes to Closed Won, set Account.Status to Customer" is a deterministic
causal claim about the system. It's not a learned causal edge; it's a
declared one. The pipeline records it as such, with `weight.source:
declared_in_system`.

### 5.5 Confidence calibration for metadata-derived cards

Cards built from metadata + documents alone carry calibrated confidence
that propagates through KnowQL and the eval framework. The rule:

- **Cards from full data + outcomes** carry full confidence; weight sources
  are `learned` where applicable.
- **Cards from metadata + documents + literature priors** carry medium
  confidence; weight sources are `literature`, `pack_default`, or
  `declared_in_system`.
- **Cards from documents only** carry lower confidence; weight sources are
  `hypothesized` with low priors.

The header of every card carries a `confidence_tier` field reflecting this:
`high`, `medium`, or `low`. Downstream queries can filter by tier
("show me only high-confidence causal claims") and the response synthesizer
explicitly flags lower-tier evidence in prose ("this attribution rests on
literature priors that have not been validated against your data").

This calibration is what prevents the system from speaking with false
authority when it's reasoning from metadata alone.

### 5.6 What the pipeline tells the user

When a tenant runs the pipeline in metadata-only mode, the run summary
explicitly reports what could and couldn't be done:

```
Pipeline run 2026-05-07 — tenant acme_corp

Sources processed:
  warehouse_full:    csod (314 tables, 8.2M rows profiled)
  api_with_data:     servicenow (47 tables, 12K samples)
  api_metadata_only: salesforce (89 sObjects, no data sampled per policy)
  documents_only:    24 policy PDFs

Operators run:
  ✓ Profilers          (warehouse, servicenow only)
  ✓ Correlators        (warehouse, servicenow only)
  ✗ Causal discovery   (insufficient data for salesforce)
  ✓ Document chunking  (all documents)
  ✓ NER + claims       (all documents)
  ✓ Metadata extraction (salesforce: 89 object_types, 1240 properties,
                                     312 link_types, 47 validation_rules,
                                     18 workflow rules → causal_rules)
  ✓ Weight learning    (warehouse outcomes only — 14 edges refit)

Confidence tier distribution:
  high:    342 cards (warehouse-derived with outcomes)
  medium:  847 cards (servicenow + salesforce metadata + documents)
  low:     106 cards (documents-only inferences)

HITL queue: 23 items pending review.
```

Reporting this transparently is part of the design. Customers using the
system in metadata-only mode need to know what they're getting and what
they're missing.

---

## 6. Operator Behavior Matrix by Source Mode

The matrix specifying which operators run in which modes, what they produce,
and how they degrade.

| Operator                  | Mode A: Warehouse | Mode B: API+Data | Mode C: Metadata | Mode D: Docs |
| ------------------------- | ----------------- | ---------------- | ---------------- | ------------ |
| **Schema Profiler**       | Full              | Full             | Full             | N/A          |
| **Column Profiler**       | Full              | Sample-bounded    | Skip — emit "stats unavailable" artifact | Skip |
| **Table Profiler**        | Full              | Sample-bounded    | Skip             | Skip         |
| **Dataset Profiler**      | Full              | Limited (cross-object queries restricted) | Skip | Skip         |
| **Linear Correlator**     | Full              | Sample-bounded    | Skip             | Skip         |
| **Mixed Correlator**      | Full              | Sample-bounded    | Skip             | Skip         |
| **Causal Structure Discovery** | Full        | Sample-bounded; flag as "from sample" | Skip — use pack causal priors only | Skip |
| **Document Chunker**      | When docs present | When docs present | When docs present | Full         |
| **NER Pipeline**          | Run on docs       | Run on docs       | Run on docs and metadata fields | Full         |
| **Claim Extractor**        | Run on docs       | Run on docs       | Run on docs and metadata descriptions | Full         |
| **Entity Linker**          | Full              | Full              | Full              | Full         |
| **Metadata Extractor**     | When schemas accessible | Full from API | Full from API | Skip         |
| **API Validation Rule Extractor** | N/A         | Full             | Full              | Skip         |
| **API Workflow Extractor** | N/A              | Full             | Full              | Skip         |
| **Weight Learner**         | Full when outcomes available | Limited | Skip — keep priors | Skip |
| **Outcome Collector**      | When available    | When API exposes  | Skip              | Skip         |

Every operator is implemented to degrade cleanly. A `Column Profiler` invoked
on a metadata-only source returns a `StatsUnavailableArtifact` — not a
failure, not silent emptiness, but an explicit "this couldn't be computed,
here's why." Downstream operators that consume profiler output check for
this artifact and adjust their behavior.

### 6.1 Implementation discipline

Three rules that operators must follow to support graceful degradation:

1. **Declare data dependencies explicitly.** An operator's input artifact
   types make clear what data it needs. The framework checks at planning
   time whether those inputs are available; if not, the operator is marked
   "skipped (insufficient inputs)" and the pipeline continues.

2. **Emit explicit "unavailable" artifacts.** When an operator can run
   structurally but produces no useful output (correlator with empty data),
   it emits an explicit artifact recording why. This is critical for
   downstream confidence calibration — the absence of a finding is itself
   information.

3. **Carry the confidence tier through.** Every artifact emitted by every
   operator carries a confidence tier derived from its inputs. A correlator
   running on full warehouse data emits `high`; on a sample of 1,000 rows,
   `medium`; on metadata only, the operator doesn't run at all.

---

## 7. Worked Examples

Two end-to-end examples showing how a tenant's mixed sources flow through
the pipeline.

### 7.1 LMS+security tenant with mixed sources

Tenant `acme_corp` has:
- CSOD in Snowflake (Mode A — full warehouse access)
- Salesforce CRM (Mode B — metadata + sample data within rate limits)
- Cornerstone Compliance (Mode C — metadata only, no data egress permitted)
- A folder of internal policy PDFs (Mode D — documents only)

After a daily run, the ontology contains:

```
From CSOD (Mode A):
  - 47 object_type cards (full schema + profiles)
  - 312 property_type cards (with full distributions)
  - 89 link_type cards (FKs verified empirically)
  - 14 causal_edge cards with weight.source: learned (from outcomes)
  → confidence_tier: high

From Salesforce (Mode B):
  - 89 object_type cards (sObject metadata)
  - 1240 property_type cards (field metadata + sample distributions)
  - 312 link_type cards (declared lookup/master-detail relationships)
  - 47 validation_rule cards (from validation rule metadata)
  - 18 causal_rule cards (from workflow rules)
  - 12 correlation findings (from sample data)
  → confidence_tier: medium

From Cornerstone (Mode C):
  - 23 object_type cards (API metadata only)
  - 187 property_type cards (no distributions)
  - 56 link_type cards (declared relationships)
  - 8 derivation_rule cards (from formula fields)
  - 0 correlation findings (no data)
  → confidence_tier: medium

From policy PDFs (Mode D):
  - 47 concept cards (extracted from documents)
  - 31 derivation_rule cards (from policy text)
  - 12 governance cards (from access policy text)
  - 23 hypothesized causal_edge cards (literature priors)
  → confidence_tier: medium-to-low
```

A KnowQL query asking "what causes overdue cybersecurity training?" returns
results synthesizing across all four sources. The response surfaces:
- Learned causal edges from CSOD warehouse data (high confidence — flagged
  inline)
- Hypothesized edges from policy documents (medium confidence — flagged
  inline as "from policy text")
- Workflow rule chains from Salesforce (declared, not learned — flagged
  inline)

Each contribution is calibrated; the user sees what the system knows and
what it's reasoning from.

### 7.2 eClinical tenant with metadata-only API

Tenant `pharma_co` has:
- Veeva Vault (Mode C — strict data egress policy; metadata only)
- Medidata Rave (Mode B — limited API sampling permitted)
- A folder of FDA submission documents and SOPs (Mode D)
- A folder of MedDRA, RxNorm, SNOMED imports (pack-shipped CDM)

After a run, the ontology contains:

```
Pack-shipped (eclinical_v1.0):
  - 142 seed concepts (AdverseEvent, ProtocolDeviation, etc.)
  - 89 CDM object_types from OMOP and CDISC SDTM
  - 234 hypothesized causal_edges from literature
    (Naranjo, Bradford Hill, drug-drug interactions from DrugBank)
  → confidence_tier: medium

From Veeva Vault (Mode C):
  - 76 object_type cards (Vault metadata)
  - 540 property_type cards (no distributions)
  - 192 link_type cards
  - 34 validation_rule cards
  - 0 correlations or learned weights
  → confidence_tier: medium

From Medidata (Mode B):
  - 42 object_type cards (with sample-derived distributions)
  - 287 property_type cards
  - 78 link_type cards
  - 12 correlations (sample-bounded)
  → confidence_tier: medium

From FDA submissions and SOPs (Mode D):
  - 89 concept cards (clinical and regulatory concepts)
  - 47 validation_rule cards (from SOPs)
  - 23 hypothesized causal_edges (from clinical literature in submissions)
  → confidence_tier: medium-to-low
```

Even with no warehouse data, the system has a working ontology because the
eClinical pack ships substantial seed knowledge and the API metadata is
rich. Causal queries return literature-backed hypothesized edges with
explicit "this is hypothesized, not learned from your data" caveats. As
outcome data eventually flows in (post-marketing surveillance feeds, trial
outcome batches), the Weight Learner promotes hypothesized edges to learned
ones.

This is the trajectory that matters: **the system is useful from day one
even without data**, and gets better as data arrives. Customers don't need
to wait for a data lake to start using it.

---

## 8. Open Questions

1. **Confidence tier resolution policy.** When a card is updated by
   sources at different confidence tiers, what's the resulting tier?
   Probably `max(input_tiers)` — if any contributing source is high-
   confidence, the card is high-confidence overall. But edge cases need
   thought (a single high-confidence source contradicted by ten medium-
   confidence sources).

2. **API metadata refresh cadence.** Some metadata changes (new fields, new
   validation rules) are operationally important and should propagate
   quickly. Daily ingestion may be too slow for a customer who just added a
   compliance-relevant field. A "metadata-only fast lane" running hourly
   for API sources is reasonable but adds operational complexity.

3. **Sample data quotas.** Mode B operators sample within rate budgets, but
   how aggressively? Tighter sampling means lower confidence; looser means
   higher cost and rate-limit risk. Per-source quotas in tenant config is
   the working answer; tuning them is a per-tenant exercise.

4. **Document confidence inheritance.** A `confidence_in_source` declared
   in document `_metadata.yaml` should propagate to derived cards, but how?
   A `low`-source document should not produce `high`-tier cards. The rule
   is probably: a card's tier is bounded above by the lowest-confidence
   source contributing to it. Worth verifying this is the right rule.

5. **Pack version pinning vs. tracking latest.** Some tenants will want to
   pin to a specific pack version for stability (especially in regulated
   environments where pack changes need re-validation). Others will want
   to auto-track the latest. Both are reasonable; the config supports both.

6. **Customer-extension API for source adapters.** Customers with proprietary
   systems will need to write their own adapters. The platform exposes the
   `ApiMetadataAdapter` interface, but the security and review model for
   customer-written adapters needs design — sandboxing, code review, version
   management.

7. **Metadata-only NER calibration.** GLiNER on field descriptions has
   different precision/recall characteristics than on prose documents.
   Probably need separate threshold tuning for the metadata-extraction pass
   versus the document-extraction pass. Worth measuring once we have real
   tenant data.

8. **What about synthetic data generation.** When sample data is forbidden
   but the tenant wants better correlations, could synthetic data
   conforming to the schema's metadata constraints help? Probably no for
   causal reasoning (synthetic correlations are not real correlations) but
   maybe yes for system testing. Out of scope for now but worth noting.

---

## 9. Phased Delivery

**Phase 1 — Three-tier configuration loader.** Platform / pack / tenant
config schemas in Pydantic. Resolution and merge logic. Validation and
fail-fast behavior. End: a tenant config can be loaded, validated, and used
to start a pipeline run. ~1 week.

**Phase 2 — Local storage convention and document lifecycle.** Directory
layout enforcement, file watcher for inbox-to-staging, `_metadata.yaml`
schema and parser, lifecycle state transitions (staging → processed →
archive). End: documents flow through the lifecycle correctly, pipeline
reads only from staging. ~2 weeks.

**Phase 3 — Mode A operators (warehouse full).** All operators implemented
against warehouse data. Standalone backend. End: end-to-end pipeline runs
on a warehouse-resident tenant with full data access. ~4 weeks.

**Phase 4 — Metadata extractors and Mode C support.** API metadata adapters
for Salesforce, Workday, ServiceNow as initial set. Metadata extractors
producing object_type / property_type / link_type / validation_rule /
causal_rule cards. Operator degradation logic for missing-data cases.
Confidence tier propagation through the pipeline. End: a metadata-only
tenant produces a useful ontology. ~4 weeks.

**Phase 5 — Mode B (API with data).** Sample data fetchers with rate-limit
awareness. Sample-bounded profilers and correlators. Confidence tier
adjustments for sampled data. End: hybrid tenants (warehouse + API + data
+ metadata + documents) work end-to-end. ~3 weeks.

**Phase 6 — Pack versioning and upgrade flow.** Pack-versioned cards,
upgrade diff generator, tenant override resolution, upgrade approval flow.
End: tenants can upgrade packs without losing customizations. ~2 weeks.

**Phase 7 — Custom adapter API.** Documented `ApiMetadataAdapter` interface,
adapter testing harness, security and review model for customer adapters.
End: customers can extend the system with adapters for proprietary APIs.
~2 weeks.

Total to GA: ~18 weeks. The pipeline is usable from Phase 3 (~7 weeks);
metadata-only support comes online at Phase 4 (~11 weeks); the full mixed-
mode story ships at Phase 5 (~14 weeks).

The critical-path is Phase 4 — metadata-only is the deployment mode that
opens the market for tenants who can't or won't expose data. Everything
else builds on that.
