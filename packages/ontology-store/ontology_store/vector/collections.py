"""Qdrant collection definitions.

Captures the 10 collections specced in `hierarchy_persistence_and_ingestion_spec.md`
§5 and `retrieval_v2_spec.md` §5.3. Each has:

- `tier_id`                : a stable id used in code (`hier_t4_assets`, `cards`, ...)
- `name_template`          : format string producing the actual collection name
                             (env-scoped for the spine tiers; tenant-scoped for cards / sql_pairs / historical_qa)
- `payload_filters`        : payload keys the indexer guarantees are populated and
                             which retrieval clients can filter on; these get
                             Qdrant payload indexes created automatically
- `narrative_fields`       : the docstring of which source fields feed embedding text
- `vector_size`            : embedding dim (default 1536, matching text-embedding-3-small)
- `distance`               : 'Cosine'

`resolve_collection_name(spec, env=..., tenant_id=...)` substitutes the right
template variable. Spine tiers are env-scoped; the three per-tenant collections
are tenant-scoped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

DistanceKind = Literal["Cosine", "Dot", "Euclid"]


@dataclass(frozen=True)
class CollectionSpec:
    tier_id: str
    name_template: str
    scope: Literal["env", "tenant"]
    description: str
    payload_filters: tuple[str, ...] = field(default_factory=tuple)
    narrative_fields: tuple[str, ...] = field(default_factory=tuple)
    vector_size: int = 1536
    distance: DistanceKind = "Cosine"


# ── Spine tiers (env-scoped) ────────────────────────────────────────────

HIER_T0_ORGS = CollectionSpec(
    tier_id="hier_t0_orgs",
    name_template="hier_t0_orgs_{env}",
    scope="env",
    description="Organizations.",
    payload_filters=("industry", "compliance_regimes", "org_size_class", "org_id"),
    narrative_fields=("display_name", "business_context", "industry", "sub_industry"),
)

HIER_T1_SOURCES = CollectionSpec(
    tier_id="hier_t1_sources",
    name_template="hier_t1_sources_{env}",
    scope="env",
    description="Source instances.",
    payload_filters=("org_id", "kind", "role", "environment"),
    narrative_fields=("display_name", "purpose", "business_context", "role"),
)

HIER_T2_CATALOGS = CollectionSpec(
    tier_id="hier_t2_catalogs",
    name_template="hier_t2_catalogs_{env}",
    scope="env",
    description="Catalogs.",
    payload_filters=("org_id", "source_id", "lifecycle_stage", "access_pattern"),
    narrative_fields=("display_name", "description", "purpose", "notes"),
)

HIER_T3_SCHEMAS = CollectionSpec(
    tier_id="hier_t3_schemas",
    name_template="hier_t3_schemas_{env}",
    scope="env",
    description="Schemas.",
    payload_filters=("org_id", "source_id", "catalog_uid", "domain_tags", "lifecycle_stage"),
    narrative_fields=("display_name", "description", "purpose", "domain_tags"),
)

HIER_T4_ASSETS = CollectionSpec(
    tier_id="hier_t4_assets",
    name_template="hier_t4_assets_{env}",
    scope="env",
    description="All asset subtypes (table, view, materialized_view, api_endpoint, function, metric); filter by asset_kind.",
    payload_filters=(
        "asset_kind",
        "lifecycle_stage",
        "effective_sensitivity_class",
        "domain_tags",
        "concepts",
        "key_areas",
        "causal_relations",
        "org_id",
        "source_id",
        "catalog_uid",
        "schema_rk",
        "primary_object_type",
        "implements_interfaces",
        # Phase 2+ — populated from the relations TBox + per-asset stats.
        # Lets retrieval filter assets by predicate vocabulary or by whether
        # any inferred/causal signal has landed on them.
        "relation_predicates",         # list[str] — predicates this asset participates in
        "has_inferred_relationships",  # bool
        "causal_node_count",           # int
        "rich_description_present",    # bool
    ),
    narrative_fields=("name", "description", "purpose", "view_definition_summary", "bound_card_excerpt"),
)

HIER_T5_FIELDS = CollectionSpec(
    tier_id="hier_t5_fields",
    name_template="hier_t5_fields_{env}",
    scope="env",
    description="All field subtypes (column, api_field, function_parameter, metric_dimension); filter by field_kind.",
    payload_filters=(
        "field_kind", "is_pii", "pii_categories", "parent_rk", "org_id",
        # Phase 1 — populated from foundry profiling output (column_stat).
        # `null_rate_bucket` / `distinct_count_bucket` are discretized so
        # Qdrant payload indexes can serve range queries efficiently
        # ('high', 'med', 'low') — actual floats stay in Postgres column_stat.
        "cardinality_tier",        # low / medium / high / identifier
        "null_rate_bucket",        # low (<10%) / med (10-50%) / high (>50%)
        "distinct_count_bucket",   # low / med / high / unique
        "is_business_key",         # bool — from ColumnSemanticsEnricher
        "semantic_unit",            # currency_usd / datetime / identifier / ...
    ),
    narrative_fields=("name", "description", "semantic_unit", "bound_card_field_mention"),
)

HIER_T6_CODES = CollectionSpec(
    tier_id="hier_t6_codes",
    name_template="hier_t6_codes_{env}",
    scope="env",
    description="Code lists + values.",
    payload_filters=("parent_rk", "parent_kind", "is_closed", "org_id"),
    narrative_fields=("name", "description", "value_labels_joined"),
)


# ── Per-tenant collections ──────────────────────────────────────────────

CARDS = CollectionSpec(
    tier_id="cards",
    name_template="cards_{tenant_id}",
    scope="tenant",
    description="Semantic-layer cards (object_type, interface, causal_node, derived_state, action, metric, event, instruction).",
    payload_filters=("layer", "kind", "markings", "refs", "origin", "deprecated"),
    narrative_fields=("body", "aliases"),
)

SQL_PAIRS = CollectionSpec(
    tier_id="sql_pairs",
    name_template="sql_pairs_{tenant_id}",
    scope="tenant",
    description="(question, sql, instructions) pairs.",
    payload_filters=(
        "references_asset_rks",
        "concepts",
        "key_areas",
        "source_provenance",
        "valid_for_lifecycle",
    ),
    narrative_fields=("question", "instructions"),
)

HISTORICAL_QA = CollectionSpec(
    tier_id="historical_qa",
    name_template="historical_qa_{tenant_id}",
    scope="tenant",
    description="Past Q&A turns from the MCP ask tool.",
    payload_filters=("cited_asset_rks", "used_intent", "satisfaction", "asked_at"),
    narrative_fields=("question",),
)


# ── Event-sourced collections ──────────────────────────────────────────
#
# All four follow the same envelope (see `events.EventEnvelope`):
#   event_id, event_kind, subject_rk, produced_at, provenance, run_id,
#   confidence, supersedes, + event-kind-specific keys hoisted from payload.
#
# Append-only: every enrichment / validation / human edit emits a NEW point.
# Retrieval aggregates events into "current state" at query time by grouping
# on subject_rk and ranking by produced_at / confidence / event_kind.
#
# Doc-per-row collections (HIER_T*, CARDS, SQL_PAIRS, HISTORICAL_QA) coexist
# with these — assets are stable identities, inferences are evolving facts.

CAUSAL_EVENTS = CollectionSpec(
    tier_id="causal_events",
    name_template="causal_events_{tenant_id}",
    scope="tenant",
    description=(
        "Append-only event log for causal candidates and their lifecycle "
        "(proposed → validated/rejected/inconclusive → promoted_to_claim). "
        "One point per event; aggregate by (subject_ref, predicate, object_ref) "
        "at read time."
    ),
    payload_filters=(
        # Envelope
        "event_kind", "subject_rk", "produced_at", "provenance",
        "run_id", "confidence",
        # Causal-specific (hoisted from payload for filtering)
        "predicate", "subject_ref", "object_ref",
        "status", "org_id", "source_id",
    ),
    narrative_fields=(
        "mechanism_hint", "rationale", "evidence_columns_joined",
    ),
)

RELATION_EVENTS = CollectionSpec(
    tier_id="relation_events",
    name_template="relation_events_{tenant_id}",
    scope="tenant",
    description=(
        "Append-only event log for predicate vocabulary observations and "
        "canonicalizations. Each invocation of `foundry.relations.induce_schema` "
        "emits one `relation_type_canonicalized` event per induced predicate; "
        "every contributing lineage_edge emits a `predicate_attached_to_edge`."
    ),
    payload_filters=(
        "event_kind", "subject_rk", "produced_at", "provenance",
        "run_id", "confidence",
        "predicate", "domain", "range_type",
        "evidence_count_bucket",   # low (1-5) / med (6-50) / high (50+)
        "org_id",
    ),
    narrative_fields=(
        "predicate", "domain", "range_type", "surfaces_joined",
    ),
)

PROTECTION_EVENTS = CollectionSpec(
    tier_id="protection_events",
    name_template="protection_events_{tenant_id}",
    scope="tenant",
    description=(
        "Append-only event log for data-protection observations: PII "
        "classification per column, sensitivity assignment per asset, "
        "RLS/CLS suggestions. Lets compliance Q&A answer 'what changed about "
        "X's PII handling between dates Y and Z' from the event stream."
    ),
    payload_filters=(
        "event_kind", "subject_rk", "produced_at", "provenance",
        "run_id", "confidence",
        "asset_rk", "is_pii", "pii_categories",
        "sensitivity_class", "org_id",
    ),
    narrative_fields=(
        "rationale", "rls_predicates_joined", "cls_columns_joined",
    ),
)

CARD_EVENTS = CollectionSpec(
    tier_id="card_events",
    name_template="card_events_{tenant_id}",
    scope="tenant",
    description=(
        "Append-only event log for card lifecycle (authored, revised, "
        "deprecated, aliased). The current state of a card stays in the "
        "doc-per-row `CARDS` collection; this log keeps the history."
    ),
    payload_filters=(
        "event_kind", "subject_rk", "produced_at", "provenance",
        "run_id",
        "card_kind", "card_id", "org_id", "deprecated",
    ),
    narrative_fields=("body_excerpt", "title", "aliases_joined"),
)


# ── Registry helpers ────────────────────────────────────────────────────

_ALL_SPECS: tuple[CollectionSpec, ...] = (
    # Spine (doc-per-row)
    HIER_T0_ORGS,
    HIER_T1_SOURCES,
    HIER_T2_CATALOGS,
    HIER_T3_SCHEMAS,
    HIER_T4_ASSETS,
    HIER_T5_FIELDS,
    HIER_T6_CODES,
    # Per-tenant authoring + history
    CARDS,
    SQL_PAIRS,
    HISTORICAL_QA,
    # Per-tenant event logs (append-only)
    CAUSAL_EVENTS,
    RELATION_EVENTS,
    PROTECTION_EVENTS,
    CARD_EVENTS,
)


def all_collection_specs() -> tuple[CollectionSpec, ...]:
    return _ALL_SPECS


def resolve_collection_name(spec: CollectionSpec, *, env: str | None = None, tenant_id: str | None = None) -> str:
    """Substitute template vars to produce the actual collection name."""
    if spec.scope == "env":
        if not env:
            raise ValueError(f"Collection {spec.tier_id!r} is env-scoped; env is required")
        return spec.name_template.format(env=env)
    if spec.scope == "tenant":
        if not tenant_id:
            raise ValueError(f"Collection {spec.tier_id!r} is tenant-scoped; tenant_id is required")
        # Slug-safe (Qdrant collection names: lowercase, alnum, hyphens/underscores)
        safe = tenant_id.replace("-", "_").lower()
        return spec.name_template.format(tenant_id=safe)
    raise ValueError(f"Unknown scope: {spec.scope!r}")
