"""CausalDependencyEnricher — propose causal participation + candidates for an asset.

Indexing-time causal enrichment is necessarily LLM-driven and evidence-anchored
because we only have schema + metadata, not the underlying data. Statistical
causal discovery (PC / LiNGAM / PCMCI / Granger — in `ontology_foundry.causal/`)
is a SEPARATE downstream concern that runs against sample data once an asset is
ingested. This stage does what's tractable at index time:

  1. For each tenant `causal_node` card, ask the LLM: does this asset participate
     in this causal node, and if so, in what role (subject / outcome /
     mediator / moderator)? Which columns are the signals?

  2. Propose causal candidates with evidence pointing to specific columns +
     a controlled-vocab predicate. Each candidate carries a confidence and a
     mechanism_hint. Candidates land in side_output for downstream review +
     persistence in `causal_candidate` (when the table lands).

Outputs:
  - Updates MDL `model.causal_relations[]` with causal_node ids the asset
    participates in (any role).
  - Attaches a richer `causal_participation` block to the MDL (extras allowed).
  - `side_output["causal_candidates"]` carries proposed causal edges.
  - `side_output["proposed_causal_node_drafts"]` carries any NEW causal_node
    suggestions for human review (never auto-applied — vocab discipline).

No-clobber:
  - Existing `causal_relations[]` entries are preserved.
  - Asset-level human-authored causal participation (if present via prior runs)
    is preserved.

Predicate vocabulary (controlled — others are dropped at filter time):
  causes, caused_by, leading_indicator_of, lagging_indicator_of,
  moderates, mediates, precedes, enables, inhibits, correlates_with
"""
from __future__ import annotations

import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from ontology_foundry.llm.provider import ModelProvider, ModelRole
from ontology_foundry.llm.transform import llm_structured_transform

from ontology_pipeline.enrich.base import EnrichmentContext, EnrichmentResult
from ontology_pipeline.models import GeneratedMDL

logger = logging.getLogger(__name__)

LLM_PROVENANCE = "llm_causal_dependency"

ROLES: tuple[str, ...] = ("subject", "outcome", "mediator", "moderator")

CAUSAL_PREDICATES: tuple[str, ...] = (
    "causes",
    "caused_by",
    "leading_indicator_of",
    "lagging_indicator_of",
    "moderates",
    "mediates",
    "precedes",
    "enables",
    "inhibits",
    "correlates_with",
)


# ───────────────────────────────────────────────────────────────────────────
# LLM response schema
# ───────────────────────────────────────────────────────────────────────────

class _Participation(BaseModel):
    causal_node_id: str
    role: Literal["subject", "outcome", "mediator", "moderator"]
    column_signals: list[str] = Field(
        default_factory=list,
        description="Columns in THIS asset that support this participation.",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


class _CausalCandidate(BaseModel):
    subject_ref: str = Field(
        description="Either '<concept_id>' or '<concept_id>.<field>' or '<asset_rk>.<column>'."
    )
    predicate: str = Field(description=f"One of: {', '.join(CAUSAL_PREDICATES)}")
    object_ref: str = Field(
        description="Either a causal_node card_id, or '<concept_id>' / '<concept_id>.<field>'."
    )
    evidence_columns: list[str] = Field(
        default_factory=list,
        description="Columns in this asset that justify the candidate.",
    )
    mechanism_hint: str = Field(default="", description="One-sentence proposed mechanism.")
    confidence: float = Field(ge=0.0, le=1.0)


class _CausalNodeDraft(BaseModel):
    """An LLM-proposed NEW causal_node — never auto-applied; goes to a review queue."""
    proposed_id: str
    title: str
    body_excerpt: str = ""
    suggested_subject_refs: list[str] = Field(default_factory=list)
    suggested_outcome_refs: list[str] = Field(default_factory=list)
    rationale: str = ""


class _CausalResponse(BaseModel):
    participations: list[_Participation] = Field(default_factory=list)
    candidates: list[_CausalCandidate] = Field(default_factory=list)
    proposed_causal_node_drafts: list[_CausalNodeDraft] = Field(default_factory=list)
    rationale: str = ""


# ───────────────────────────────────────────────────────────────────────────
# Public stage
# ───────────────────────────────────────────────────────────────────────────

class CausalDependencyEnricher:
    """LLM-driven causal participation + candidate generation."""

    name = "causal_dependency"

    def __init__(
        self,
        *,
        role: ModelRole = ModelRole.RELATION_EXTRACTOR,
        known_causal_node_ids: list[str] | None = None,
        known_causal_node_excerpts: dict[str, str] | None = None,
        vocab_source: Any | None = None,
        min_confidence_for_relation: float = 0.5,
        propose_new_causal_nodes: bool = False,
    ) -> None:
        """
        Args:
            known_causal_node_ids: vocab the LLM picks from for participation +
                candidate objects. When empty, only `proposed_causal_node_drafts`
                are produced (in side_output) — no MDL updates.
            known_causal_node_excerpts: optional excerpts for prompt grounding;
                keyed by causal_node id.
            vocab_source: optional `VocabSource` (see
                `ontology_pipeline.cards.vocab_source`). If provided, the
                enricher pulls `causal_node` cards from this source the first
                time it needs them and caches the result for subsequent
                `apply()` calls. Takes precedence over the explicit
                `known_causal_node_*` args when both are supplied — those args
                stay supported for callers that don't have a DB session yet.
            min_confidence_for_relation: per-participation threshold for
                writing into MDL.causal_relations[].
            propose_new_causal_nodes: if True, prompt the LLM to draft new
                causal_node cards when existing vocab doesn't fit. Drafts land
                in side_output for human review — never auto-applied.
        """
        self._role = role
        self._explicit_ids = list(known_causal_node_ids or [])
        self._explicit_excerpts = dict(known_causal_node_excerpts or {})
        self._vocab_source = vocab_source
        self._known_ids: list[str] = []
        self._known_excerpts: dict[str, str] = {}
        self._vocab_loaded = False
        self._min_confidence = min_confidence_for_relation
        self._propose_new = propose_new_causal_nodes
        if vocab_source is None:
            # No source — explicit args are the only vocab. Treat as loaded.
            self._known_ids = list(self._explicit_ids)
            self._known_excerpts = dict(self._explicit_excerpts)
            self._vocab_loaded = True

    def refresh_vocab(self) -> None:
        """Force a fresh vocab pull from the configured source.

        Call between pipeline runs if cards may have changed in Postgres
        since the enricher was constructed. No-op when no `vocab_source`
        was provided.
        """
        if self._vocab_source is None:
            return
        try:
            sv = self._vocab_source.load()
            cn_summaries = list(getattr(sv, "causal_nodes", []) or [])
            self._known_ids = [c.id for c in cn_summaries]
            self._known_excerpts = {c.id: c.body_excerpt for c in cn_summaries}
            self._vocab_loaded = True
            logger.info(
                "CausalDependencyEnricher loaded %d causal_node ids from %s",
                len(self._known_ids), type(self._vocab_source).__name__,
            )
        except Exception as exc:  # noqa: BLE001 — defense-in-depth
            logger.warning(
                "CausalDependencyEnricher: failed to load vocab from %s: %s; "
                "falling back to explicit known_causal_node_ids",
                type(self._vocab_source).__name__, exc,
            )
            self._known_ids = list(self._explicit_ids)
            self._known_excerpts = dict(self._explicit_excerpts)
            self._vocab_loaded = True

    def _ensure_vocab_loaded(self) -> None:
        if not self._vocab_loaded:
            self.refresh_vocab()

    def apply(self, mdl: GeneratedMDL, ctx: EnrichmentContext) -> EnrichmentResult:
        result = EnrichmentResult(stage_name=self.name)
        if not mdl.models or ctx.provider is None:
            if ctx.provider is None:
                result.warnings.append("no LLM provider; causal enrichment skipped")
            return result
        self._ensure_vocab_loaded()
        if not self._known_ids and not self._propose_new:
            result.warnings.append(
                "no known causal_node vocab; pass known_causal_node_ids= or set propose_new=True"
            )
            return result

        t0 = time.perf_counter()
        model = mdl.models[0]
        prompt = self._build_prompt(model=model, ctx=ctx)
        try:
            response = llm_structured_transform(
                ctx.provider, self._role, prompt, _CausalResponse,
            )
            result.llm_calls += 1
        except Exception as exc:
            logger.warning("CausalDependencyEnricher LLM failed for %s: %s", model.rk, exc)
            result.warnings.append(f"llm error: {exc}")
            return result

        # ── Apply participations (filter to known vocab + threshold) ──
        existing_relations = set(model.causal_relations or [])
        valid_participations: list[dict[str, Any]] = []
        for p in response.participations:
            if p.causal_node_id not in self._known_ids:
                continue  # vocab discipline
            if p.confidence < self._min_confidence:
                continue
            if p.role not in ROLES:
                continue
            valid_participations.append({
                "causal_node_id": p.causal_node_id,
                "role": p.role,
                "column_signals": list(p.column_signals),
                "confidence": float(p.confidence),
                "rationale": p.rationale,
            })
            # Mirror into MDL.causal_relations[] (union with prior)
            if p.causal_node_id not in existing_relations:
                model.causal_relations.append(p.causal_node_id)
                existing_relations.add(p.causal_node_id)

        if valid_participations:
            # Attach the richer per-role block as a top-level extra on the model
            participation_block = {
                "asset_rk": model.rk,
                "items": valid_participations,
                "provenance": LLM_PROVENANCE,
            }
            try:
                object.__setattr__(model, "causal_participation", participation_block)
            except Exception:
                pass
            result.fields_updated.append(f"causal_relations[+{len(valid_participations)}]")
            result.fields_updated.append("causal_participation")

        # ── Side-output candidates (post-filtered to controlled predicates) ──
        # Each candidate carries the subject asset's human-readable surface so
        # downstream consumers (event narratives, retrieval prompts) can
        # reason about WHICH asset / column / domain is involved without
        # parsing rk URIs. See `asset_surface.render_asset_surface()`.
        from ontology_pipeline.enrich.asset_surface import (
            build_column_lookup,
            render_asset_one_liner,
            render_asset_surface,
        )
        asset_surface = render_asset_surface(model)
        asset_one_liner = render_asset_one_liner(model)
        # Map column_name → {type, description, brief, is_pii, semantic_unit, …}
        # so the event builder can render every evidence column with full
        # native COMMENT ON COLUMN text + PII/semantic flags. The lookup is
        # built once per asset; cheaper than re-walking model.columns inside
        # the per-candidate loop. Cost: ~200-500 bytes per asset in the
        # event payload, well worth the narration density.
        column_lookup = build_column_lookup(model)
        filtered_candidates: list[dict[str, Any]] = []
        for c in response.candidates:
            if c.predicate not in CAUSAL_PREDICATES:
                continue  # vocab discipline
            # The object_ref must either resolve to a known causal_node OR be
            # a concept-shape ref (caller resolves downstream).
            subject_col_name = _column_from_ref(c.subject_ref)
            object_col_name = _column_from_ref(c.object_ref)
            filtered_candidates.append({
                "asset_rk": model.rk,
                "asset_name": model.name,
                "asset_description": model.description,
                "subject_ref": c.subject_ref,
                "subject_asset_surface": asset_surface,
                "subject_one_liner": asset_one_liner,
                "subject_column_brief": (
                    column_lookup.get(subject_col_name, {}).get("brief")
                    if subject_col_name else None
                ),
                "predicate": c.predicate,
                "object_ref": c.object_ref,
                # For causal_node-shaped object_refs we can't surface a table
                # description; the object_ref IS the card id and the card body
                # is the surface. Cross-asset enricher fills both sides.
                "object_one_liner": c.object_ref,
                "object_column_brief": (
                    column_lookup.get(object_col_name, {}).get("brief")
                    if object_col_name else None
                ),
                "evidence_columns": list(c.evidence_columns),
                # Per-evidence-column lookup: full type + description + flags
                # so downstream narration shows what each anchor IS.
                "subject_column_lookup": column_lookup,
                "mechanism_hint": c.mechanism_hint,
                "confidence": float(c.confidence),
                "status": "proposed",
                "provenance": LLM_PROVENANCE,
            })
        if filtered_candidates:
            result.side_output["causal_candidates"] = filtered_candidates
            result.fields_updated.append(f"causal_candidates[{len(filtered_candidates)} proposed]")

        # ── Side-output causal_node drafts (never auto-applied) ──
        if self._propose_new and response.proposed_causal_node_drafts:
            result.side_output["proposed_causal_node_drafts"] = [
                {
                    "proposed_id": d.proposed_id,
                    "title": d.title,
                    "body_excerpt": d.body_excerpt,
                    "suggested_subject_refs": list(d.suggested_subject_refs),
                    "suggested_outcome_refs": list(d.suggested_outcome_refs),
                    "rationale": d.rationale,
                    "source_asset_rk": model.rk,
                    "provenance": LLM_PROVENANCE,
                }
                for d in response.proposed_causal_node_drafts
            ]
            result.fields_updated.append(
                f"proposed_causal_node_drafts[{len(response.proposed_causal_node_drafts)}]"
            )

        result.wall_time_ms = int((time.perf_counter() - t0) * 1000)
        return result

    # ── Prompt builder ─────────────────────────────────────────────────

    def _build_prompt(self, *, model: Any, ctx: EnrichmentContext) -> str:
        from ontology_pipeline.enrich.grounding import format_tabular_grounding
        columns_block = self._format_columns(model)
        existing_concepts = ", ".join(model.concepts or []) or "(none)"
        existing_key_areas = ", ".join(model.key_areas or []) or "(none)"
        # Causal reasoning benefits from distribution / cardinality info to
        # judge candidate signals (e.g. is `due_date` a measure or a key?).
        grounding = format_tabular_grounding(ctx, max_sample_rows=6)

        if self._known_ids:
            vocab_block = "\n".join(
                f"  - {cid}" + (f" — {self._known_excerpts[cid]}"
                               if cid in self._known_excerpts else "")
                for cid in self._known_ids
            )
        else:
            vocab_block = "  (none — propose new causal_node drafts in `proposed_causal_node_drafts`)"

        propose_new_block = (
            "Additionally, if existing causal_node vocab does not fit, draft NEW "
            "causal_node candidates in `proposed_causal_node_drafts`. These will be "
            "reviewed by a human before being added — do NOT use them in `participations`."
        ) if self._propose_new else (
            "Do NOT propose new causal_node ids. Use ONLY the ids listed in CANDIDATE "
            "causal_node CARDS for participations."
        )

        return f"""You are a causal-inference assistant analyzing a data asset's schema for likely causal dependencies. Output JSON only.

ASSET:
  rk:     {model.rk}
  name:   {model.name}
  kind:   {"view" if model.is_view else "table"}
  source: {ctx.source_id}
  schema: {ctx.schema_name}
  description: {model.description or "(none)"}
{grounding}
COLUMNS:
{columns_block}

CURRENT BINDINGS:
  concepts:  {existing_concepts}
  key_areas: {existing_key_areas}

CANDIDATE causal_node CARDS (the controlled vocabulary):
{vocab_block}

CAUSAL PREDICATES (controlled vocab — use ONLY these for candidate predicates):
  {", ".join(CAUSAL_PREDICATES)}

ROLES (for participations):
  - subject   — this asset's state CHANGES drive the causal node (a "cause-side" record)
  - outcome   — this asset's state IS AFFECTED by upstream changes
  - mediator  — sits between subject and outcome
  - moderator — strength of the cause→effect link depends on values in this asset

TASKS:
1. For each causal_node in the vocabulary, decide whether this asset participates
   in it and in which role. Cite the specific columns that support the role.
   Skip causal_nodes where there is no signal — empty `participations` is valid.

2. Propose CAUSAL CANDIDATES — concrete subject → predicate → object edges with
   column-level evidence. Each candidate must:
     - Use a predicate from the controlled vocab.
     - Anchor evidence to specific columns in this asset.
     - Carry a confidence (0..1) and a one-sentence mechanism_hint.
     - object_ref should be a causal_node card id when possible.

3. {propose_new_block}

Output JSON STRICTLY:
{{
  "participations": [
    {{ "causal_node_id": "<from vocab>", "role": "subject|outcome|mediator|moderator",
       "column_signals": ["<col>", ...], "confidence": 0.0..1.0, "rationale": "..." }}
  ],
  "candidates": [
    {{ "subject_ref": "<concept_or_concept.column>", "predicate": "<from vocab>",
       "object_ref": "<causal_node_id or concept>", "evidence_columns": ["<col>", ...],
       "mechanism_hint": "...", "confidence": 0.0..1.0 }}
  ],
  "proposed_causal_node_drafts": [
    {{ "proposed_id": "snake_case_id", "title": "Title Case", "body_excerpt": "...",
       "suggested_subject_refs": ["concept_id", ...],
       "suggested_outcome_refs": ["concept_id", ...], "rationale": "..." }}
  ],
  "rationale": "one-paragraph overall reasoning"
}}

Guardrails:
- Be conservative. Empty arrays are valid — and PREFERRED when uncertain.
- Confidence calibration: 0.5 = "plausible, would need data to verify";
                         0.8 = "schema strongly suggests this";
                         0.95 = "structurally explicit (e.g., status columns + timestamps)".
- Do NOT invent column names. Use ONLY the columns listed above.
- For tables that are pure lookup tables (single dimension), participations and
  candidates should typically be empty.
"""

    @staticmethod
    def _format_columns(model: Any) -> str:
        lines: list[str] = []
        for c in model.columns:
            extras = c.properties.model_extra or {}
            semantic = extras.get("semantic_unit")
            is_pii = extras.get("is_pii")
            label = f"  - {c.name} ({c.type})"
            if c.properties.is_primary_key:
                label += " [PK]"
            if semantic:
                label += f" [{semantic}]"
            if is_pii:
                label += " [PII]"
            if c.properties.description:
                label += f" — {c.properties.description}"
            lines.append(label)
        return "\n".join(lines) if lines else "  (no columns)"


def _column_from_ref(ref: str | None) -> str | None:
    """Pull the column suffix out of a `<asset_rk>.<column>` ref.

    Returns None for bare asset_rks (no `.column` suffix) or bare causal_node
    ids (no `://` scheme).
    """
    if not ref or "://" not in ref:
        return None
    last_slash = ref.rfind("/")
    tail = ref[last_slash + 1:]
    if "." not in tail:
        return None
    return ref.rpartition(".")[2] or None
