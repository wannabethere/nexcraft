"""Builders that turn Postgres rows into Qdrant event points.

Each `build_*_event` function takes a domain row (an ORM object OR the dict
shape that flows through reindex_queue payloads) and returns:

    (EventEnvelope, narrative_text, extra_payload)

The indexer then calls `HierarchyVectorIndexer.append_*_event(...)` with these.

Why the split between this module and `narrative.py`:

  - `narrative.py` builds doc-per-row text + payload for the SPINE collections
    (orgs/sources/catalogs/schemas/assets/fields/codes). One point per identity.
  - `event_narrative.py` builds event-shaped points for the four `*_events`
    collections. Many points per identity over time.

Each builder also folds the source row's mutable fields into the event
payload so retrieval can filter / facet without joining back to Postgres
for the common case.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ontology_store.vector.events import EventEnvelope, EventKind


# ───────────────────────────────────────────────────────────────────────────
# CAUSAL_EVENTS builders
# ───────────────────────────────────────────────────────────────────────────


def build_causal_candidate_event(
    *,
    row: Any,
    run_id: str | None = None,
    org_id: str | None = None,
    source_id: str | None = None,
) -> tuple[EventEnvelope, str, dict[str, Any]]:
    """Translate a `causal_candidate` Postgres row into a CAUSAL_EVENTS point.

    The row's current `status` drives the event_kind: a 'proposed' row emits
    `CAUSAL_CANDIDATE_PROPOSED`, 'validated' emits `CAUSAL_CANDIDATE_VALIDATED`,
    etc. The same Postgres row therefore emits multiple events over its
    lifetime — each one is a NEW point keyed by a fresh event_id.
    """
    status = (getattr(row, "status", None) or "proposed").lower()
    kind = _CAUSAL_STATUS_TO_KIND.get(status, EventKind.CAUSAL_CANDIDATE_PROPOSED)

    produced_at = _coerce_dt(
        getattr(row, "validated_at", None)
        or getattr(row, "updated_at", None)
        or getattr(row, "created_at", None),
    )
    envelope = EventEnvelope(
        event_id=EventEnvelope.new_id(kind=kind, at=produced_at),
        event_kind=kind,
        subject_rk=getattr(row, "asset_rk", "") or "",
        produced_at=produced_at,
        provenance=getattr(row, "provenance", "llm_causal_dependency"),
        run_id=run_id,
        confidence=_coerce_float(getattr(row, "confidence", None)),
        supersedes=None,  # set by callers chaining a correction
        payload={
            "predicate": getattr(row, "predicate", ""),
            "subject_ref": getattr(row, "subject_ref", ""),
            "object_ref": getattr(row, "object_ref", ""),
            "status": status,
            "org_id": org_id,
            "source_id": source_id,
        },
    )

    narrative = _causal_narrative(row)
    # Hoist asset name + description onto the Qdrant payload too so retrieval
    # can filter by subject_asset_name without parsing rks. These are the
    # SAME values that appear in the narrative, just structured for filter
    # use rather than embedded text use.
    extra = {
        "mechanism_hint": getattr(row, "mechanism_hint", None) or "",
        "rationale": getattr(row, "rationale", None) or "",
        "evidence_columns_joined": ", ".join(
            getattr(row, "evidence_columns", []) or [],
        ),
        "subject_asset_name": getattr(row, "asset_name", None) or "",
        "subject_asset_description": getattr(row, "asset_description", None) or "",
        "subject_one_liner": getattr(row, "subject_one_liner", None) or "",
        "object_one_liner": getattr(row, "object_one_liner", None) or "",
    }
    return envelope, narrative, extra


_CAUSAL_STATUS_TO_KIND: dict[str, EventKind] = {
    "proposed": EventKind.CAUSAL_CANDIDATE_PROPOSED,
    "validated": EventKind.CAUSAL_CANDIDATE_VALIDATED,
    "rejected": EventKind.CAUSAL_CANDIDATE_REJECTED,
    "inconclusive": EventKind.CAUSAL_CANDIDATE_INCONCLUSIVE,
    "promoted_to_claim": EventKind.CAUSAL_CANDIDATE_PROMOTED_TO_CLAIM,
}


def _causal_narrative(row: Any) -> str:
    """Compose the embedded text for one causal-candidate event.

    Maximizes narration density so downstream LLM reasoning has full context:
    every column referenced — anchor columns AND evidence columns — is shown
    with its type and native COMMENT ON COLUMN description. The principle:
    be verbose at narration time, terse at storage time. Bigger embeddings
    cost cents; better LLM reasoning saves whole turns.

    Format::

        Subject: users_core (All registered users in the organisation.)
                 column: due_date (TIMESTAMP) — The due date for the training when 'Fixed Date' is selected.
        Predicate: leading_indicator_of
        Object:  transcript_core (Per-employee training completion history.)
                 column: completed_date (TIMESTAMP) — When the user marked the training complete.

        Evidence (subject columns):
          - due_date (TIMESTAMP) — The due date for the training…
          - employee_id (INTEGER) → public.users.user_id — FK to users.

        Evidence (object columns):
          - completed_date (TIMESTAMP) — When the user marked complete.

        Mechanism: Overdue training drives compliance gap accumulation.
        Rationale: ...

        --- Subject asset surface ---
        <full surface>

        --- Object asset surface ---
        <full surface>

    Falls back to bare rk + bare column names when surface / lookup fields
    aren't present (legacy callers).
    """
    subject_ref = getattr(row, "subject_ref", "") or ""
    pred = getattr(row, "predicate", "") or ""
    obj_ref = getattr(row, "object_ref", "") or ""

    subject_one_liner = getattr(row, "subject_one_liner", None) or ""
    object_one_liner = getattr(row, "object_one_liner", None) or ""
    subject_surface = getattr(row, "subject_asset_surface", None) or ""
    object_surface = getattr(row, "object_asset_surface", None) or ""
    subject_col_brief = getattr(row, "subject_column_brief", None) or ""
    object_col_brief = getattr(row, "object_column_brief", None) or ""
    subject_col_lookup = getattr(row, "subject_column_lookup", None) or {}
    object_col_lookup = getattr(row, "object_column_lookup", None) or {}
    evidence_subj = list(getattr(row, "evidence_columns", []) or [])
    evidence_obj = list(getattr(row, "evidence_object_columns", []) or [])

    parts: list[str] = []
    if subject_one_liner or object_one_liner:
        # Rich human-readable shape — anchor columns with full briefs
        subject_col = _column_from_ref(subject_ref)
        object_col = _column_from_ref(obj_ref)
        parts.append(
            f"Subject: {subject_one_liner or subject_ref}"
            + (
                f"\n         column: {subject_col_brief or subject_col}"
                if subject_col else ""
            )
        )
        parts.append(f"Predicate: {pred}")
        parts.append(
            f"Object:  {object_one_liner or obj_ref}"
            + (
                f"\n         column: {object_col_brief or object_col}"
                if object_col else ""
            )
        )
    elif subject_ref or pred or obj_ref:
        # Fallback for callers that don't supply surface fields
        parts.append(f"{subject_ref} -[{pred}]-> {obj_ref}")

    # Evidence columns — render with full descriptions when the lookup is
    # available. Falls back to bare names otherwise.
    if evidence_subj:
        parts.append(_render_evidence(evidence_subj, subject_col_lookup, "subject columns"))
    if evidence_obj:
        parts.append(_render_evidence(evidence_obj, object_col_lookup, "object columns"))

    mech = getattr(row, "mechanism_hint", None)
    if mech:
        parts.append(f"Mechanism: {mech}")
    rat = getattr(row, "rationale", None)
    if rat:
        parts.append(f"Rationale: {rat}")

    if subject_surface:
        parts.append("\n--- Subject asset surface ---\n" + subject_surface)
    if object_surface and object_surface != subject_surface:
        parts.append("\n--- Object asset surface ---\n" + object_surface)

    return "\n".join(parts) if parts else "(empty causal candidate)"


def _render_evidence(
    column_names: list[str],
    column_lookup: dict[str, Any],
    label: str,
) -> str:
    """Render an evidence-columns block with descriptions when available."""
    if not column_names:
        return ""
    lines = [f"Evidence ({label}):"]
    for name in column_names:
        entry = column_lookup.get(name) if isinstance(column_lookup, dict) else None
        if isinstance(entry, dict) and entry.get("brief"):
            lines.append(f"  - {entry['brief']}")
        else:
            lines.append(f"  - {name}")
    return "\n".join(lines)


def _column_from_ref(ref: str) -> str | None:
    """Extract the column suffix from `<asset_rk>.<column>` if present.

    Returns None for bare-rk (no column) or causal_node-id (no scheme) refs.
    """
    if not ref or "://" not in ref:
        return None
    last_slash = ref.rfind("/")
    tail = ref[last_slash + 1:]
    if "." not in tail:
        return None
    return ref.rpartition(".")[2] or None


# ───────────────────────────────────────────────────────────────────────────
# RELATION_EVENTS builders
# ───────────────────────────────────────────────────────────────────────────


def build_relation_type_event(
    *,
    row: Any,
    run_id: str | None = None,
) -> tuple[EventEnvelope, str, dict[str, Any]]:
    """Translate a `relation_type` Postgres row into a RELATION_EVENTS point.

    Always emits `RELATION_TYPE_CANONICALIZED` — this is the "after" state
    that `foundry.relations.induce_schema` produced. If callers want to
    record the raw observations BEFORE canonicalization they should emit
    `RELATION_TYPE_OBSERVED` directly from the relationship enricher
    (one event per inferred edge surface).
    """
    produced_at = _coerce_dt(
        getattr(row, "updated_at", None) or getattr(row, "created_at", None),
    )
    predicate = getattr(row, "predicate", "") or ""
    domain = getattr(row, "domain", "") or ""
    range_type = getattr(row, "range_type", "") or ""
    subject_rk = f"{predicate}:{domain}->{range_type}"
    evidence_count = int(getattr(row, "evidence_count", 0) or 0)

    envelope = EventEnvelope(
        event_id=EventEnvelope.new_id(
            kind=EventKind.RELATION_TYPE_CANONICALIZED, at=produced_at,
        ),
        event_kind=EventKind.RELATION_TYPE_CANONICALIZED,
        subject_rk=subject_rk,
        produced_at=produced_at,
        provenance=getattr(row, "provenance", "induce_schema"),
        run_id=run_id,
        confidence=_coerce_float(getattr(row, "confidence", None)),
        payload={
            "predicate": predicate,
            "domain": domain,
            "range_type": range_type,
            "evidence_count_bucket": _bucket_count(evidence_count),
            "org_id": getattr(row, "org_id", None),
        },
    )

    surfaces = getattr(row, "surfaces", None) or ""
    if isinstance(surfaces, list):
        surfaces_str = ", ".join(surfaces)
    else:
        surfaces_str = surfaces
    narrative = (
        f"Predicate '{predicate}' canonicalises ({surfaces_str or 'no aliases'}); "
        f"domain={domain}, range={range_type}; observed in {evidence_count} edge(s)."
    )
    extra = {"surfaces_joined": surfaces_str}
    return envelope, narrative, extra


def build_predicate_attached_event(
    *,
    from_rk: str,
    to_rk: str,
    edge_kind: str,
    predicate: str,
    domain: str,
    range_type: str,
    run_id: str | None = None,
    org_id: str | None = None,
    from_one_liner: str | None = None,
    to_one_liner: str | None = None,
    from_surface: str | None = None,
    to_surface: str | None = None,
    from_column: str | None = None,
    to_column: str | None = None,
    from_column_brief: str | None = None,
    to_column_brief: str | None = None,
) -> tuple[EventEnvelope, str, dict[str, Any]]:
    """One event per (lineage_edge -> relation_type) attachment.

    When the caller passes the human-readable one-liners + surfaces + FK
    column briefs, the narrative embeds all of them. The FK column briefs in
    particular let downstream consumers see WHAT the join key is (e.g.,
    "user_id (INTEGER) → public.users_core.user_id — Surrogate key for the
    user.") rather than just the column name.
    """
    produced_at = datetime.now(timezone.utc)
    envelope = EventEnvelope(
        event_id=EventEnvelope.new_id(
            kind=EventKind.PREDICATE_ATTACHED_TO_EDGE, at=produced_at,
        ),
        event_kind=EventKind.PREDICATE_ATTACHED_TO_EDGE,
        subject_rk=f"{from_rk}-[{edge_kind}]->{to_rk}",
        produced_at=produced_at,
        provenance="induce_schema",
        run_id=run_id,
        confidence=None,
        payload={
            "predicate": predicate, "domain": domain, "range_type": range_type,
            "org_id": org_id,
            "from_column": from_column or "", "to_column": to_column or "",
        },
    )
    if from_one_liner or to_one_liner:
        narrative_lines = [
            f"Edge: {from_one_liner or from_rk}"
        ]
        if from_column:
            narrative_lines.append(
                f"      column: {from_column_brief or from_column}"
            )
        narrative_lines.append(f"  -[{edge_kind}]->")
        narrative_lines.append(f"  {to_one_liner or to_rk}")
        if to_column:
            narrative_lines.append(
                f"      column: {to_column_brief or to_column}"
            )
        narrative_lines.append(
            f"Predicate: '{predicate}' (domain={domain}, range={range_type})."
        )
        narrative = "\n".join(narrative_lines)
        # Surfaces (full schemas) appended for richer retrieval matches
        if from_surface:
            narrative += f"\n\n--- From-side surface ---\n{from_surface}"
        if to_surface:
            narrative += f"\n\n--- To-side surface ---\n{to_surface}"
    else:
        col_suffix = ""
        if from_column or to_column:
            col_suffix = f" (join: {from_column or '?'} → {to_column or '?'})"
        narrative = (
            f"Edge {from_rk} -[{edge_kind}]-> {to_rk}{col_suffix} attached to "
            f"predicate '{predicate}' ({domain} → {range_type})."
        )
    return envelope, narrative, {
        "surfaces_joined": "",
        "from_one_liner": from_one_liner or "",
        "to_one_liner": to_one_liner or "",
        "from_column": from_column or "",
        "to_column": to_column or "",
    }


# ───────────────────────────────────────────────────────────────────────────
# PROTECTION_EVENTS builders
# ───────────────────────────────────────────────────────────────────────────


def build_data_protection_event(
    *,
    row: Any,
    run_id: str | None = None,
    org_id: str | None = None,
) -> tuple[EventEnvelope, str, dict[str, Any]]:
    """Translate a `data_protection_hint` row into a PROTECTION_EVENTS point.

    Status 'proposed' → `DATA_PROTECTION_HINT_PROPOSED`.
    Status 'applied'  → `DATA_PROTECTION_HINT_APPLIED`.
    """
    status = (getattr(row, "status", None) or "proposed").lower()
    kind = (
        EventKind.DATA_PROTECTION_HINT_APPLIED if status == "applied"
        else EventKind.DATA_PROTECTION_HINT_PROPOSED
    )
    produced_at = _coerce_dt(
        getattr(row, "updated_at", None) or getattr(row, "created_at", None),
    )
    asset_rk = getattr(row, "asset_rk", "") or ""
    envelope = EventEnvelope(
        event_id=EventEnvelope.new_id(kind=kind, at=produced_at),
        event_kind=kind,
        subject_rk=asset_rk,
        produced_at=produced_at,
        provenance=getattr(row, "provenance", "llm_data_protection"),
        run_id=run_id,
        confidence=None,
        payload={
            "asset_rk": asset_rk,
            "org_id": org_id,
        },
    )
    rls = getattr(row, "rls_predicates", None) or []
    cls = getattr(row, "cls_columns", None) or []
    asset_one_liner = getattr(row, "asset_one_liner", None) or ""
    asset_surface = getattr(row, "asset_surface", None) or ""
    column_lookup = getattr(row, "column_lookup", None) or {}
    narrative = _protection_narrative(
        rls=rls, cls=cls,
        rationale=getattr(row, "rationale", None),
        asset_one_liner=asset_one_liner,
        asset_surface=asset_surface,
        column_lookup=column_lookup,
    )
    extra = {
        "rls_predicates_joined": "; ".join(rls),
        "cls_columns_joined": ", ".join(cls),
        "rationale": getattr(row, "rationale", None) or "",
        "asset_name": getattr(row, "asset_name", None) or "",
        "asset_one_liner": asset_one_liner,
    }
    return envelope, narrative, extra


def _protection_narrative(
    *,
    rls: list[str],
    cls: list[str],
    rationale: Any,
    asset_one_liner: str = "",
    asset_surface: str = "",
    column_lookup: dict[str, Any] | None = None,
) -> str:
    parts: list[str] = []
    if asset_one_liner:
        parts.append(f"Asset: {asset_one_liner}")
    if rls:
        parts.append(f"RLS suggestions: {'; '.join(rls)}")
    if cls:
        # Render each CLS column with its full brief (type + native
        # description + PII categories / sensitivity if classified).
        if column_lookup:
            parts.append("CLS suggestions (column-level masking targets):")
            for col_name in cls:
                entry = column_lookup.get(col_name) if isinstance(column_lookup, dict) else None
                if isinstance(entry, dict) and entry.get("brief"):
                    parts.append(f"  - {entry['brief']}")
                else:
                    parts.append(f"  - {col_name}")
        else:
            parts.append(f"CLS suggestions: {', '.join(cls)}")
    if rationale:
        parts.append(f"Rationale: {rationale}")
    if asset_surface:
        parts.append(f"\n--- Asset surface ---\n{asset_surface}")
    return "\n".join(parts) if parts else "(empty protection hint)"


def build_pii_classification_event(
    *,
    column_rk: str,
    is_pii: bool,
    pii_categories: list[str],
    sensitivity_class: str | None,
    provenance: str = "llm_data_protection",
    run_id: str | None = None,
    org_id: str | None = None,
) -> tuple[EventEnvelope, str, dict[str, Any]]:
    """One column → one PII classification event."""
    produced_at = datetime.now(timezone.utc)
    envelope = EventEnvelope(
        event_id=EventEnvelope.new_id(kind=EventKind.PII_CLASSIFIED, at=produced_at),
        event_kind=EventKind.PII_CLASSIFIED,
        subject_rk=column_rk,
        produced_at=produced_at,
        provenance=provenance,
        run_id=run_id,
        confidence=None,
        payload={
            "is_pii": bool(is_pii),
            "pii_categories": list(pii_categories or []),
            "sensitivity_class": sensitivity_class,
            "org_id": org_id,
        },
    )
    narrative = (
        f"Column {column_rk}: is_pii={is_pii}, "
        f"categories={','.join(pii_categories or []) or '(none)'}, "
        f"sensitivity={sensitivity_class or '(unset)'}"
    )
    return envelope, narrative, {
        "rls_predicates_joined": "",
        "cls_columns_joined": "",
        "rationale": "",
    }


# ───────────────────────────────────────────────────────────────────────────
# CARD_EVENTS builders
# ───────────────────────────────────────────────────────────────────────────


def build_card_event(
    *,
    row: Any,
    is_new: bool,
    run_id: str | None = None,
) -> tuple[EventEnvelope, str, dict[str, Any]]:
    """Translate a `card` row into a CARD_EVENTS point.

    `is_new=True` for a freshly-inserted card → `CARD_AUTHORED`.
    `is_new=False` for an updated card → `CARD_REVISED`.
    Deprecation flips emit `CARD_DEPRECATED` via the dedicated builder.
    """
    if getattr(row, "deprecated", False):
        kind = EventKind.CARD_DEPRECATED
    else:
        kind = EventKind.CARD_AUTHORED if is_new else EventKind.CARD_REVISED
    produced_at = _coerce_dt(
        getattr(row, "updated_at", None) or getattr(row, "created_at", None),
    )
    card_id = getattr(row, "card_id", "") or ""
    card_kind = getattr(row, "kind", "") or ""
    org_id = getattr(row, "org_id", None)
    subject_rk = f"{org_id}:{card_kind}:{card_id}"

    envelope = EventEnvelope(
        event_id=EventEnvelope.new_id(kind=kind, at=produced_at),
        event_kind=kind,
        subject_rk=subject_rk,
        produced_at=produced_at,
        provenance=getattr(row, "origin", "tenant"),
        run_id=run_id,
        confidence=None,
        payload={
            "card_kind": card_kind,
            "card_id": card_id,
            "org_id": org_id,
            "deprecated": bool(getattr(row, "deprecated", False)),
        },
    )

    body = getattr(row, "body", "") or ""
    title = getattr(row, "title", None) or ""
    aliases = getattr(row, "aliases", None) or []
    narrative = (
        f"{kind.value} — {card_kind}:{card_id}"
        + (f" '{title}'" if title else "")
        + f"\n\n{body[:1000]}"  # truncate at 1KB for embedding budget
    )
    return envelope, narrative, {
        "body_excerpt": body[:300],
        "title": title,
        "aliases_joined": ", ".join(aliases),
    }


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _coerce_dt(value: Any) -> datetime:
    """Best-effort datetime coercion. Defaults to now() in UTC."""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bucket_count(n: int) -> str:
    """Discretise evidence_count for Qdrant payload-index friendliness.

    Qdrant's payload range filters are efficient, but discrete buckets make
    common queries ("predicates with at least medium support") cheaper to
    express and reason about. Mirror the same scheme on column_stat bucketing.
    """
    if n <= 5:
        return "low"
    if n <= 50:
        return "med"
    return "high"
