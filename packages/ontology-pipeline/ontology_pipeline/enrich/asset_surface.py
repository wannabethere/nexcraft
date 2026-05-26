"""Human-readable asset surface for prompts + event narratives.

Asset_rks like `postgres://csod-local.csod_learning/public/users_core` are
optimized for storage / routing, not LLM reasoning. When the same asset
appears in a downstream prompt or in a vector-search corpus, the readable
shape — name, description, concepts, key_areas, and annotated columns — is
what carries semantic meaning.

This module owns the canonical surface rendering. Used by:

  - CausalDependencyEnricher                → includes `subject_asset_surface`
                                              on each emitted candidate
  - CrossAssetCausalEnricher                → includes subject + object
                                              surfaces; prompt also uses
                                              the same rendering
  - RelationshipInferenceEnricher           → includes `from_asset_surface`
  - workers/event_narrative builders        → use surfaces in narrative text
                                              + payload

The rendering is intentionally token-conscious: ~250–500 tokens per asset
for a typical 20-column table. Columns are listed with type + native
description; PK marker + bound semantic_unit shown when present.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ontology_pipeline.models import MDLModel


def render_asset_surface(
    model: "MDLModel",
    *,
    max_columns: int | None = None,
    include_concepts: bool = True,
    include_key_areas: bool = True,
) -> str:
    """Render an MDL model as a compact human-readable surface.

    Format::

        users_core (rk=postgres://csod-local.csod_learning/public/users_core)
          description: All registered users in the organisation.
          concepts: employee
          key_areas: workforce, training_compliance
          columns:
            - user_id (INTEGER) [PK] — Surrogate key for the user.
            - email (TEXT) [email] — Primary contact email.
            - department_id (INTEGER) — FK to public.department.

    Args:
        model: an `MDLModel` (post-build, may already carry annotation fields).
        max_columns: cap column lines for prompt-budget control. `None` = all.
        include_concepts / include_key_areas: drop these lines when False;
            useful for the very first enrichment pass where annotations
            haven't been computed yet.
    """
    lines: list[str] = [f"{model.name} (rk={model.rk})"]
    if model.description:
        lines.append(f"  description: {_clean(model.description)}")
    if include_concepts:
        concepts = list(getattr(model, "concepts", None) or [])
        lines.append(f"  concepts: {', '.join(concepts) if concepts else '(none)'}")
    if include_key_areas:
        key_areas = list(getattr(model, "key_areas", None) or [])
        lines.append(f"  key_areas: {', '.join(key_areas) if key_areas else '(none)'}")
    causal_relations = list(getattr(model, "causal_relations", None) or [])
    if causal_relations:
        lines.append(f"  causal_relations: {', '.join(causal_relations)}")

    cols = list(model.columns)
    if max_columns is not None and max_columns >= 0:
        cols = cols[:max_columns]
    if cols:
        lines.append("  columns:")
        for c in cols:
            lines.append(f"    - {_column_line(c)}")
        if max_columns is not None and max_columns < len(model.columns):
            lines.append(
                f"    - … ({len(model.columns) - max_columns} more columns omitted)"
            )
    return "\n".join(lines)


def render_column_brief(col: Any) -> str:
    """One-line column rendering for inline use in narratives.

    Format examples::

        `due_date (TIMESTAMP) — The due date for the training when 'Fixed Date' is selected.`
        `user_id (INTEGER) [PK] [identifier] → public.users.user_id — Surrogate key for the user.`
        `ssn (TEXT) [PII] — Social Security Number.`

    Maximizes narration density: type, PK flag, semantic_unit, FK target, PII
    flag, and the native COMMENT ON COLUMN description all appear on one line.
    Designed to be embedded in event narratives so a downstream LLM reasoning
    about a causal mechanism sees WHAT each evidence column actually is.
    """
    return _column_line(col)


def build_column_lookup(model: "MDLModel") -> dict[str, dict[str, Any]]:
    """`{col_name: {type, description, brief, is_primary_key, references, is_pii,
    pii_categories, sensitivity_class, semantic_unit, business_meaning}}`.

    Returned dicts are JSON-serialisable. Use this when an enricher emits a
    candidate / relationship and the event consumer needs to look up column
    details without holding a reference to the (mutable, not-JSON-safe) MDL.
    """
    out: dict[str, dict[str, Any]] = {}
    for c in model.columns:
        props = getattr(c, "properties", None)
        extra = (getattr(props, "model_extra", None) or {}) if props else {}
        out[c.name] = {
            "name": c.name,
            "type": c.type,
            "description": getattr(props, "description", None) if props else None,
            "is_primary_key": bool(getattr(props, "is_primary_key", False)) if props else False,
            "references": getattr(props, "references", None) if props else None,
            # PII + semantics — only present after their enrichers run.
            "is_pii": extra.get("is_pii"),
            "pii_categories": extra.get("pii_categories"),
            "sensitivity_class": extra.get("sensitivity_class"),
            "semantic_unit": extra.get("semantic_unit"),
            "business_meaning": extra.get("business_meaning"),
            "is_business_key": extra.get("is_business_key"),
            "brief": _column_line(c),
        }
    return out


def render_evidence_block(
    *,
    label: str,
    column_names: list[str],
    column_lookup: dict[str, dict[str, Any]],
) -> str:
    """Render a labeled evidence-columns section with full descriptions.

    Format::

        Evidence (subject columns):
          - due_date (TIMESTAMP) — The due date for the training…
          - employee_id (INTEGER) → public.users.user_id — FK to users.

    Falls back to bare names when the lookup is missing entries (defensive
    against asynchronous schema drift between enricher and event emission).
    """
    if not column_names:
        return ""
    lines = [f"Evidence ({label}):"]
    for name in column_names:
        entry = column_lookup.get(name)
        if entry and entry.get("brief"):
            lines.append(f"  - {entry['brief']}")
        else:
            lines.append(f"  - {name}")
    return "\n".join(lines)


def render_asset_one_liner(model: "MDLModel") -> str:
    """Single-line summary — used inside event narratives.

    Format: `users_core (All registered users in the organisation.)`
    Falls back to just the name when no description is set.
    """
    desc = (model.description or "").strip()
    if desc:
        snippet = desc.split("\n", 1)[0]
        if len(snippet) > 120:
            snippet = snippet[:117] + "…"
        return f"{model.name} ({snippet})"
    return model.name


def build_asset_lookup(models: "list[MDLModel] | list[Any]") -> dict[str, dict[str, Any]]:
    """Build a lookup keyed by asset_rk for use by sinks / post-passes.

    Each entry contains: `name`, `description`, `one_liner`, `surface` (full
    rendering), `concepts`, `key_areas`.

    Sinks pass this through to event builders so a builder can look up the
    OTHER side of a causal candidate by rk without needing the full MDL.
    """
    out: dict[str, dict[str, Any]] = {}
    for m in models:
        # Accept either a `GeneratedMDL` (with `.models[0]`) or an `MDLModel`.
        model = getattr(m, "models", None)
        if model is not None and model:
            model = model[0]
        else:
            model = m
        rk = getattr(model, "rk", None)
        if not rk:
            continue
        out[rk] = {
            "name": getattr(model, "name", ""),
            "description": getattr(model, "description", None),
            "one_liner": render_asset_one_liner(model),
            "surface": render_asset_surface(model),
            "concepts": list(getattr(model, "concepts", None) or []),
            "key_areas": list(getattr(model, "key_areas", None) or []),
        }
    return out


# ───────────────────────────────────────────────────────────────────────────
# Internals
# ───────────────────────────────────────────────────────────────────────────


def _column_line(col: Any) -> str:
    """`user_id (INTEGER) [PK] [identifier] — Surrogate key for the user.`"""
    parts = [f"{col.name} ({col.type})"]
    props = getattr(col, "properties", None)
    if props is None:
        return parts[0]
    if getattr(props, "is_primary_key", False):
        parts.append("[PK]")
    # Pull semantic_unit / is_pii from properties.model_extra when the
    # ColumnSemanticsEnricher / DataProtectionEnricher have run.
    extra = getattr(props, "model_extra", None) or {}
    semantic_unit = extra.get("semantic_unit")
    if semantic_unit:
        parts.append(f"[{semantic_unit}]")
    is_pii = extra.get("is_pii")
    if is_pii:
        parts.append("[PII]")
    references = getattr(props, "references", None)
    description = getattr(props, "description", None)
    head = " ".join(parts)
    suffix_bits: list[str] = []
    if references:
        suffix_bits.append(f"→ {references}")
    if description:
        suffix_bits.append(_clean(description))
    if suffix_bits:
        return f"{head} — {' | '.join(suffix_bits)}"
    return head


def _clean(text: str | None, max_chars: int = 300) -> str:
    """Whitespace-normalise + clip a description for one-line prompt use."""
    if not text:
        return ""
    s = " ".join(text.split())
    if len(s) > max_chars:
        s = s[: max_chars - 1].rstrip() + "…"
    return s
