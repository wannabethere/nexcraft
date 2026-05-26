"""Narrative-text and payload builders per tier.

These pure functions translate the structured Postgres view of an entity into
the (embedding_text, payload) tuple the Qdrant indexer consumes. Per the
collection specs in `vector/collections.py` and the spec at
`hierarchy_persistence_and_ingestion_spec.md` §5.2.

Builders take Pydantic / domain objects, NOT raw SQLAlchemy rows — keeps them
testable in isolation against fixture inputs.
"""
from __future__ import annotations

from typing import Any

from ontology_store.schemas import TableContext


# ───────────────────────────────────────────────────────────────────────────
# T4 — Asset narrative + payload
# ───────────────────────────────────────────────────────────────────────────

def build_asset_narrative(ctx: TableContext, *, bound_card_excerpt: str | None = None) -> str:
    """Compose the embedding text for an asset (T4).

    Composition (matches collection spec for hier_t4_assets):
      name + description + purpose + view_definition_summary + bound_card_excerpt

    `bound_card_excerpt` is a short body excerpt of the primary object_type card
    (e.g. the `employee` card body's first paragraph). When provided it sharpens
    cross-source semantic match.
    """
    parts: list[str] = [ctx.name]

    if ctx.description:
        parts.append(ctx.description)

    # column-level intent is implicit in descriptions; surface column names that
    # carry semantic-unit hints to widen the lexical signal slightly
    col_signals = []
    for c in ctx.columns:
        if c.is_primary_key:
            col_signals.append(f"PK:{c.name}")
        if c.description:
            col_signals.append(f"{c.name}: {c.description}")
    if col_signals:
        parts.append("Columns: " + "; ".join(col_signals[:12]))

    if bound_card_excerpt:
        parts.append("Concept: " + bound_card_excerpt)

    return "\n\n".join(p for p in parts if p)


def build_asset_payload(ctx: TableContext) -> dict[str, Any]:
    """Payload for an asset (T4) — matches `HIER_T4_ASSETS.payload_filters`."""
    return {
        "asset_rk": ctx.asset_rk,
        "asset_kind": ctx.asset_kind,
        "lifecycle_stage": ctx.lifecycle_stage,
        "effective_sensitivity_class": ctx.effective_sensitivity_class,
        "domain_tags": [],  # placeholder until schema_ext is wired into TableContext
        "concepts": list(ctx.concepts),
        "key_areas": list(ctx.key_areas),
        "causal_relations": list(ctx.causal_relations),
        "org_id": _org_id_from_source_id(ctx.source_id),
        "source_id": ctx.source_id,
        "catalog_uid": ctx.catalog_uid,
        "schema_rk": ctx.schema_rk,
        "primary_object_type": ctx.primary_object_type,
        "implements_interfaces": [],
        "name": ctx.name,
        "schema_name": ctx.schema_name,
    }


# ───────────────────────────────────────────────────────────────────────────
# T3 — Schema narrative + payload (lightweight; consumer uses for navigation)
# ───────────────────────────────────────────────────────────────────────────

def build_schema_narrative(
    *,
    display_name: str,
    description: str | None,
    purpose: str | None,
    domain_tags: list[str],
) -> str:
    parts: list[str] = [display_name]
    if description:
        parts.append(description)
    if purpose:
        parts.append("Purpose: " + purpose)
    if domain_tags:
        parts.append("Domain: " + ", ".join(domain_tags))
    return "\n\n".join(p for p in parts if p)


def build_schema_payload(
    *,
    schema_rk: str,
    schema_name: str,
    org_id: str,
    source_id: str,
    catalog_uid: str | None,
    lifecycle_stage: str,
    domain_tags: list[str],
) -> dict[str, Any]:
    return {
        "schema_rk": schema_rk,
        "schema_name": schema_name,
        "org_id": org_id,
        "source_id": source_id,
        "catalog_uid": catalog_uid,
        "lifecycle_stage": lifecycle_stage,
        "domain_tags": list(domain_tags),
    }


# ───────────────────────────────────────────────────────────────────────────
# T1 — Source narrative + payload
# ───────────────────────────────────────────────────────────────────────────

def build_source_narrative(
    *,
    display_name: str,
    purpose: str | None,
    business_context: str | None,
    role: str,
    entities: list[str],
) -> str:
    parts: list[str] = [display_name]
    if purpose:
        parts.append("Purpose: " + purpose)
    if business_context:
        parts.append(business_context)
    parts.append("Role: " + role)
    if entities:
        parts.append("Entities of record: " + ", ".join(entities))
    return "\n\n".join(p for p in parts if p)


def build_source_payload(
    *,
    source_id: str,
    org_id: str,
    kind: str,
    role: str,
    environment: str,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "org_id": org_id,
        "kind": kind,
        "role": role,
        "environment": environment,
    }


# ───────────────────────────────────────────────────────────────────────────
# Cards — narrative + payload
# ───────────────────────────────────────────────────────────────────────────

def build_card_narrative(*, body: str, aliases: list[str] | None = None) -> str:
    text = body or ""
    if aliases:
        text = text + "\n\nAliases: " + ", ".join(aliases)
    return text


def build_card_payload(
    *,
    layer: str,
    kind: str,
    card_id: str,
    markings: list[str] | None,
    refs: list[str] | None,
    origin: str,
    deprecated: bool,
) -> dict[str, Any]:
    return {
        "layer": layer,
        "kind": kind,
        "card_id": card_id,
        "markings": list(markings or []),
        "refs": list(refs or []),
        "origin": origin,
        "deprecated": deprecated,
    }


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────

def _org_id_from_source_id(source_id: str) -> str | None:
    """Best-effort: source_ids follow the convention <org>-<kind>-<instance>.

    The reindex worker doesn't have access to the org_id without joining
    `source`; for v1 we leave it None and let the worker hydrate from spine.
    """
    return None
