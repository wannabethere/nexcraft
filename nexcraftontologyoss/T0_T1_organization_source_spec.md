# T0 / T1 — Organization & Source Specification

**Status:** Locked 2026-05-15.
**Part of:** Data Knowledge Hierarchy series (T0 → T6).
**Supersedes:** the implicit org/source notions in `causal_ontology_foundry_design.md` (tenant configs) and in `genieml/data/sql_meta/*/project_metadata.json` (which today conflates source instance, catalog, and "project").

---

## 1. Scope

This document locks the two highest tiers of the Data Knowledge Hierarchy:

- **T0 — Organization.** The customer org. Everything below it is scoped to it.
- **T1 — Source.** A logical instance of a connected system (one Snowflake account, one Salesforce org, one ServiceNow instance).

T0/T1 records are **declarative**. They describe what *is*, not what the system *enforces*. They are read by the ontology routing layer, the governance configuration layer, and the LLM as context. They are not enforcement boundaries on their own.

**Out of scope here:**
- T2 (catalog), T3 (schema), T4 (asset), T5 (field), T6 (value) — covered in subsequent specs.
- The internals of MDL files (`mdl_*.json`). MDLs remain the source of truth for T2–T5; T0/T1 records sit *above* them. MDL enrichment is a separate thread.
- The `governance_profile` object itself — referenced from T0 regions, specified elsewhere.

---

## 2. T0 — Organization

### 2.1 Definition

An **Organization** is the customer tenant. It owns sources, defines language/locale defaults, declares compliance regime, and supplies the top-level business context that downstream tiers and the LLM inherit.

There is exactly one Organization per tenant. Multi-tenant deployments hold many Organization records; cross-org references are forbidden.

### 2.2 Fields

| Field | Type | Req | Notes |
|---|---|---|---|
| `org_id` | slug | ✓ | Stable, unique. Used as foreign key everywhere downstream. Convention: lowercase kebab-case (`acme-corp`). |
| `display_name` | string | ✓ | UI label. |
| `legal_name` | string | — | For compliance/contract surfaces. |
| `industry` | enum | ✓ | See §2.4 vocab. Drives default ontology pack selection. |
| `sub_industry` | enum | — | Refinement within industry. |
| `headquarters` | object | ✓ | `{ country (ISO-3166-α2), region, city, timezone (IANA tz) }`. |
| `operating_regions[]` | array | ✓ (≥1) | One record per operating region. See §2.3. The HQ region must appear here. |
| `primary_language` | BCP-47 | ✓ | e.g. `en-US`, `ja-JP`. Drives default extraction language + UI. |
| `supported_languages[]` | BCP-47[] | — | Additional languages users will ask questions in. Union with per-region languages forms the active language set. |
| `locale_defaults` | object | ✓ | `{ date_format, number_format, currency (ISO-4217), week_start, fiscal_year_start_month (1–12) }`. Resolves "last quarter", "$10k", "next Friday". |
| `compliance_regimes[]` | enum[] | — | HIPAA / SOX / GDPR / PCI-DSS / FedRAMP / SOC2 / ISO27001. Drives mandatory pack inclusion + sensitivity defaults. |
| `org_size_class` | enum | — | `smb` / `mid` / `enterprise`. Informs scale assumptions in cost estimators. |
| `business_context` | text | recommended | Long-form narrative: what the org does, key processes, primary revenue motion, headline risks. **Read by the LLM as context** when resolving ambiguous questions. |
| `sources[]` | source_id[] | — | Denormalized list of T1 references owned by this org. Source records are stored separately; this list exists for traversal convenience. |
| `created_at` | timestamp | ✓ | |
| `updated_at` | timestamp | ✓ | |

### 2.3 `operating_regions[]` sub-schema

Each region defines a *governance + locale scope*. Regions are not residency boundaries (see §4.1).

| Field | Type | Req | Notes |
|---|---|---|---|
| `region_id` | slug | ✓ | Unique within the org. `us`, `eu-de`, `apac-jp`. |
| `countries[]` | ISO-3166-α2[] | ✓ | Countries covered by this region. |
| `languages[]` | BCP-47[] | ✓ (≥1) | Languages active in this region. Used by extractors + UI localization. |
| `governance_profile` | slug ref | — | Reference to a governance profile (defined in a separate spec). Defaults to the org-level profile if absent. |
| `locale_overrides` | object | — | Same shape as `locale_defaults`; partial overrides allowed (e.g., only `currency`). |
| `business_context` | text | — | Regional narrative — local operations, regulatory specifics, vendor footprint. |

### 2.4 Controlled vocabularies (seed; extensible per deployment)

- **`industry`**: `healthcare`, `financial_services`, `insurance`, `manufacturing`, `retail`, `saas`, `telecom`, `energy`, `public_sector`, `education`, `media`, `transportation`, `other`.
- **`compliance_regimes`**: `HIPAA`, `SOX`, `GDPR`, `CCPA`, `PCI-DSS`, `FedRAMP`, `SOC2`, `ISO27001`, `HITRUST`, `NIST-800-53`.

### 2.5 Example

```yaml
org_id: acme-corp
display_name: Acme Corporation
legal_name: Acme Corporation, Inc.
industry: healthcare
sub_industry: provider
headquarters:
  country: US
  region: CA
  city: San Francisco
  timezone: America/Los_Angeles
operating_regions:
  - region_id: us
    countries: [US]
    languages: [en-US, es-US]
    governance_profile: us-standard
    locale_overrides: { currency: USD, date_format: MM/DD/YYYY }
    business_context: >
      40 outpatient clinics, multi-state. HIPAA-covered entity.
  - region_id: eu-de
    countries: [DE]
    languages: [de-DE, en-US]
    governance_profile: gdpr-strict
    locale_overrides:
      currency: EUR
      date_format: DD.MM.YYYY
      week_start: monday
    business_context: >
      Munich-based subsidiary, ~12 clinicians. Separate Workday tenant; runs
      on a German billing platform that does not feed the central warehouse.
primary_language: en-US
supported_languages: [en-US, es-US, de-DE]
locale_defaults:
  date_format: MM/DD/YYYY
  number_format: "1,234.56"
  currency: USD
  week_start: sunday
  fiscal_year_start_month: 7
compliance_regimes: [HIPAA, GDPR, SOC2]
org_size_class: enterprise
business_context: >
  Acme operates 40 outpatient clinics across the US plus a small German
  subsidiary. Core revenue comes from procedural billing; key operational
  risk is staffing attrition in front-line clinical roles. Snowflake is the
  primary analytics surface; Workday is the HRIS system of record.
sources:
  - acme-snowflake-prod
  - acme-servicenow-itsm
  - acme-workday
  - acme-salesforce-mkt
created_at: 2026-01-12T00:00:00Z
updated_at: 2026-05-15T00:00:00Z
```

---

## 3. T1 — Source

### 3.1 Definition

A **Source** is a logical instance of a connected system: one Snowflake account, one Salesforce org, one CSV bundle, one S3 prefix of Parquet, one dbt project. A source is the level at which:

- Knowledge silos exist (the central claim motivating the hierarchy).
- Authentication is configured.
- Refresh cadence is declared.
- Governance defaults are applied.
- Cross-source links (T-link layer, future spec) originate or terminate.

A single physical vendor account *may* be modeled as multiple Sources when its sub-environments are logically distinct (e.g., `snowflake-prod` vs `snowflake-sandbox`). One Source MUST map to exactly one `kind`.

### 3.2 Fields

| Field | Type | Req | Notes |
|---|---|---|---|
| `source_id` | slug | ✓ | Stable, unique. Convention: `{org}-{kind}-{instance}` (`acme-snowflake-prod`). |
| `org_id` | slug | ✓ | Owning organization. |
| `region_id` | slug ref | — | Which T0 region this source is associated with for governance application. Absent ⇒ inherits org default. |
| `kind` | enum | ✓ | See §3.4 vocab. |
| `vendor_details` | object | — | `{ vendor, edition, version, region }`. Vendor-region is the *cloud* region (e.g., `us-west-2`), distinct from T0 `region_id`. |
| `instance_name` | string | ✓ | Operator-facing instance label (e.g., "Snowflake Prod NA"). |
| `display_name` | string | ✓ | UI label. |
| `environment` | enum | ✓ | `prod` / `staging` / `dev` / `sandbox`. |
| `role` | enum | ✓ | Single value. See §3.4 vocab. **Used for question routing as a strong hint, not a hard constraint.** |
| `purpose` | text (1–2 sentences) | ✓ | Concise: what this source does for the org. |
| `business_context` | text (paragraph) | recommended | Longer narrative: history, vendor rationale, scope, known quirks, deprecation plans. Read by the LLM as context. |
| `entities_of_record[]` | array | — | Canonical entities this source is authoritative for. **Human declaration; not automated.** See §3.3. |
| `entities_referenced[]` | string[] | — | Canonical entities present here as foreign references (not authoritative). Feeds the cross-source link layer. |
| `business_owner` | string | recommended | Team or person owning the system from a business perspective. |
| `technical_owner` | string | recommended | Team owning the connector / pipes. |
| `refresh_cadence` | object | — | `{ mode: streaming\|batch\|snapshot, frequency: cron-or-keyword }`. |
| `freshness_sla` | duration | — | Max acceptable staleness (e.g., `24h`, `15m`). |
| `declared_residency[]` | ISO-3166-α2[] | — | Where the underlying data is *declared* to reside. **Documentation only.** |
| `residency_check_mode` | enum | — | `off` \| `best_effort`. Default `best_effort`. See §4.1. |
| `sensitivity_class` | enum | ✓ | `public` / `internal` / `confidential` / `restricted`. Default sensitivity inherited by downstream T2–T5 assets unless overridden. |
| `pii_categories[]` | enum[] | — | `names`, `contact`, `financial`, `health`, `government_id`, `biometric`. Drives masking defaults at downstream tiers. |
| `auth_kind` | string label | — | `oauth` / `keypair` / `jdbc` / `service_account`. Label only; secrets stored separately. |
| `default_locale_overrides` | object | — | If the source itself uses a non-org-default locale (e.g., a German Workday tenant in a US org). |
| `notes` | text | — | Free-form additional knowledge. Open field — anything an operator wants future maintainers to know. |
| `created_at` | timestamp | ✓ | |
| `updated_at` | timestamp | ✓ | |

### 3.3 `entities_of_record[]` sub-schema

```yaml
entities_of_record:
  - entity: Incident
    declared_by: "Jane K. (IT Ops Lead)"
    declared_at: 2026-02-14
  - entity: Asset
    declared_by: "Jane K. (IT Ops Lead)"
    declared_at: 2026-02-14
```

`declared_by` and `declared_at` are optional but recommended — when an authority claim turns out to be wrong, future maintainers need a person to ask.

### 3.4 Controlled vocabularies (seed; extensible)

- **`kind`**: `snowflake`, `bigquery`, `redshift`, `databricks`, `postgres`, `mysql`, `sqlserver`, `oracle`, `mongodb`, `salesforce`, `servicenow`, `workday`, `sap`, `netsuite`, `hubspot`, `marketo`, `zendesk`, `jira`, `github`, `s3_parquet`, `s3_csv`, `gcs_parquet`, `csv_bundle`, `api_feed`, `dbt_project`, `looker`, `tableau`.
- **`role`**:
  - `system_of_record` — authoritative source for some canonical entity.
  - `operational_application` — runs a business process; transactional.
  - `analytical_warehouse` — derived/curated for analysis.
  - `data_lake` — raw or lightly processed landing.
  - `external_reference` — third-party reference feed (e.g., CVE, MedDRA).
  - `replica` — mirrored copy of another source.

### 3.5 Example

```yaml
source_id: acme-snowflake-prod
org_id: acme-corp
region_id: us
kind: snowflake
vendor_details:
  vendor: Snowflake
  edition: enterprise
  version: "8.x"
  region: us-west-2
instance_name: Snowflake Prod NA
display_name: Snowflake (Analytics Warehouse)
environment: prod
role: analytical_warehouse
purpose: >
  Central analytics warehouse. Curated marts for finance, clinical operations,
  and workforce analytics. Source for all BI dashboards and most ad-hoc analysis.
business_context: >
  Built out 2023. Fed nightly by Fivetran from Workday, ServiceNow, and the
  EHR; intra-day from the billing system. Marts are dbt-managed; raw schemas
  are not query-exposed to end users. Finance team treats `fact_revenue` as
  the canonical revenue surface. The `analytics.legacy_*` schemas are
  deprecated and should not be surfaced in question routing.
entities_of_record: []
entities_referenced:
  - Employee
  - Patient
  - Encounter
  - Revenue
  - Incident
business_owner: Data Platform Team
technical_owner: Data Platform Team
refresh_cadence:
  mode: batch
  frequency: "0 4 * * *"
freshness_sla: 24h
declared_residency: [US]
residency_check_mode: best_effort
sensitivity_class: confidential
pii_categories: [names, contact, health]
auth_kind: keypair
notes: |
  - `analytics.legacy_*` deprecated; exclude from routing.
  - dbt project at github.com/acme/analytics-dbt is the source of truth for mart definitions.
created_at: 2026-01-15T00:00:00Z
updated_at: 2026-05-15T00:00:00Z
```

---

## 4. Semantic posture

These are the decisions that distinguish T0/T1 from a residency-enforcement system.

### 4.1 Residency is declarative; checks are best-effort

`declared_residency` documents where data is supposed to live. The ontology system does **not** enforce residency. When `residency_check_mode: best_effort`, the planner flags cross-residency joins as annotations on the query plan and emits a side-channel log. It does not block, rewrite, or refuse.

Rationale: residency enforcement is the host platform's responsibility, not this layer's. A best-effort check has signal value (catches misconfiguration) without taking on a guarantee we cannot make.

### 4.2 Entity authority is a human declaration

`entities_of_record[]` is a **human declaration** of which entities a source is authoritative for. It is consumed by:

- The LLM, as context when resolving entity-typed questions.
- The cross-source link layer, as a strong preference signal.
- Documentation and lineage surfaces.

It is **not** consumed by an automated router as a hard constraint. Routing decisions remain LLM-mediated; the declaration informs but does not bind.

Rationale: real-world entity authority is messy (HR is in Workday, but Snowflake is fresher than Workday for some attributes because of late-arriving data), and getting this wrong silently is worse than getting it wrong loudly. Surface the declaration; let the agent and the user negotiate.

### 4.3 Regions are governance + locale scopes, not residency boundaries

`operating_regions[]` declares where the org operates and therefore which languages, locale defaults, and governance profiles apply. They do not segregate data — a single Source can hold data spanning multiple regions, and the Source's `region_id` simply names which governance profile to apply by default.

---

## 5. Relationship to existing artifacts

### 5.1 MDL (`genieml/data/sql_meta/*/mdl_*.json`)

MDL files remain the source of truth for T2 (catalog), T3 (schema), T4 (model/asset), and T5 (column/field). They are **not** rewritten by this spec. Two minimal additions are required at the MDL document root to bind a model file to T0/T1:

```json
{
  "source_id": "acme-snowflake-prod",
  "catalog": "vulnerability_management",
  "schema": "public",
  "models": [ /* unchanged */ ]
}
```

The existing `catalog` and `schema` strings become the canonical T2 and T3 references. Their *knowledge wrappers* (descriptions, purpose, lifecycle) are defined in the forthcoming T2/T3 spec and stored separately — not inside the MDL document. MDLs stay focused on schema; knowledge wrappers stay focused on knowledge.

### 5.2 `project_metadata.json`

Today this file conflates source instance, catalog, and a logical "project." Under this spec it becomes a **data product descriptor** (a candidate T-tier, see §6 open items) that lists `(source_id, catalog, schema, table)` tuples participating in a curated use case. The `source_id` reference is the new addition.

No migration is required for existing projects until the T2/T3 spec lands; this spec only locks the upstream T0/T1 vocabulary.

### 5.3 `causal_ontology_foundry_design.md` (tenant/pack/CDM)

The existing "tenant config" concept in the foundry design maps onto T0 (Organization). Where the foundry doc says *tenant*, read *Organization*. The pack/CDM layer is orthogonal — packs apply *at* an Organization, driven by `industry` + `compliance_regimes`.

The foundry design's `schema_mapping` declarations bind tenant tables to CDM entities; under the hierarchy that mapping lives at T4 (asset → canonical entity). The T1 fields `entities_of_record` / `entities_referenced` are a coarser, source-level summary of the same information, derived from T4 declarations.

---

## 6. Open items (forward references)

- **`governance_profile`** — referenced from T0 regions; full schema deferred. Will define masking rules, retention, query-time audit requirements, and role-based access.
- **Data Product tier** — whether to introduce a tier between T3 and T4 for curated multi-table use cases (today's `sql_meta` projects already look like data products). Deferred to the T2/T3 spec.
- **Semantic / metrics layer** — whether dbt models and semantic-layer metrics are first-class T4/T5 with a `derivation` provenance pointer, or live in a parallel hierarchy. Deferred.
- **T-link layer** — the cross-source linking format (entity equivalence, compositional, causal-candidate). Will be a sibling spec, not a tier.

---

## 7. Change log

| Date | Change |
|---|---|
| 2026-05-15 | Initial lock. |
