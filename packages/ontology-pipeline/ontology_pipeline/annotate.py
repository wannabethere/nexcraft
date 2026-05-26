"""Annotation enrichment — concepts / key_areas / causal_relations.

LLM proposes per-asset annotations against:
  - The tenant's `object_type` and `causal_node` card index (loaded from disk).
  - The tenant's key_areas vocabulary (YAML).

Auto-applies the LLM output without human review for the iteration phase
(per mdl_table_concept_annotation_spec.md §5.6). The result carries a
provenance record so downstream services / human edits can be tracked.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ontology_foundry.llm.provider import ModelProvider, ModelRole
from ontology_foundry.llm.transform import llm_structured_transform

from ontology_pipeline.config import SemanticLayerConfig
from ontology_pipeline.models import AssetAnnotations, GeneratedMDL, MDLModel

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# Vocabulary loading
# ───────────────────────────────────────────────────────────────────────────

class CardSummary(BaseModel):
    """Compact card representation for the LLM's candidate list."""
    id: str
    kind: str
    title: str | None = None
    body_excerpt: str

    def to_prompt_line(self) -> str:
        title_part = f" — {self.title}" if self.title else ""
        return f"  - {self.id} ({self.kind}){title_part}\n      {self.body_excerpt}"


class KeyAreaEntry(BaseModel):
    id: str
    description: str = ""

    def to_prompt_line(self) -> str:
        desc = f": {self.description}" if self.description else ""
        return f"  - {self.id}{desc}"


class SemanticVocab(BaseModel):
    """Loaded vocabulary that grounds the annotation LLM call."""
    object_types: list[CardSummary] = Field(default_factory=list)
    causal_nodes: list[CardSummary] = Field(default_factory=list)
    key_areas: list[KeyAreaEntry] = Field(default_factory=list)

    @property
    def object_type_ids(self) -> set[str]:
        return {c.id for c in self.object_types}

    @property
    def causal_node_ids(self) -> set[str]:
        return {c.id for c in self.causal_nodes}

    @property
    def key_area_ids(self) -> set[str]:
        return {k.id for k in self.key_areas}


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_EXCERPT_MAX_CHARS = 300


def load_vocab(cfg: SemanticLayerConfig) -> SemanticVocab:
    """Load object_type + causal_node cards and key_areas vocab from disk.

    Card files: `<cfg.cards_dir>/{kind}s/<id>.card.md` per semantic_layer_card_spec.md.
    Excerpts up to first ~300 chars of body for the LLM prompt budget.
    """
    vocab = SemanticVocab()
    if cfg.cards_dir is not None and cfg.cards_dir.exists():
        vocab.object_types = _load_cards(cfg.cards_dir / "object_types", kind="object_type")
        vocab.causal_nodes = _load_cards(cfg.cards_dir / "causal_nodes", kind="causal_node")
    if cfg.key_areas_vocab_path is not None and cfg.key_areas_vocab_path.exists():
        vocab.key_areas = _load_key_areas(cfg.key_areas_vocab_path)
    return vocab


def _load_cards(dir_path: Path, *, kind: str) -> list[CardSummary]:
    if not dir_path.is_dir():
        return []
    out: list[CardSummary] = []
    for path in sorted(dir_path.glob("*.card.md")):
        try:
            frontmatter, body = _parse_card_file(path)
            out.append(
                CardSummary(
                    id=frontmatter.get("id") or path.stem.replace(".card", ""),
                    kind=frontmatter.get("kind") or kind,
                    title=frontmatter.get("title"),
                    body_excerpt=_excerpt(body),
                )
            )
        except Exception as exc:
            logger.warning("Failed to parse card %s: %s", path, exc)
    return out


def _parse_card_file(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text.strip()
    fm_block, body = m.group(1), m.group(2)
    fm = yaml.safe_load(fm_block) or {}
    if not isinstance(fm, dict):
        fm = {}
    return fm, body.strip()


def _excerpt(body: str) -> str:
    body = body.strip()
    if len(body) <= _EXCERPT_MAX_CHARS:
        return body
    cut = body[:_EXCERPT_MAX_CHARS]
    last_space = cut.rfind(" ")
    if last_space > 100:
        cut = cut[:last_space]
    return cut + "…"


def _load_key_areas(path: Path) -> list[KeyAreaEntry]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("key_areas", [])
    out: list[KeyAreaEntry] = []
    for e in entries:
        if isinstance(e, str):
            out.append(KeyAreaEntry(id=e))
        elif isinstance(e, dict) and e.get("id"):
            out.append(KeyAreaEntry(id=e["id"], description=e.get("description") or ""))
    return out


# ───────────────────────────────────────────────────────────────────────────
# LLM annotation
# ───────────────────────────────────────────────────────────────────────────

class _AnnotationResponse(BaseModel):
    concepts: list[str] = Field(default_factory=list,
                                description="object_type card ids that this asset embodies (most-primary first).")
    key_areas: list[str] = Field(default_factory=list,
                                 description="key_area ids from the vocabulary that this asset serves.")
    causal_relations: list[str] = Field(default_factory=list,
                                        description="causal_node card ids this asset feeds or affects.")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""


def enrich_annotations(
    mdl: GeneratedMDL,
    *,
    vocab: SemanticVocab,
    provider: ModelProvider | None,
    role: ModelRole = ModelRole.RELATION_EXTRACTOR,
    source_model: str | None = None,
    concepts_source: str = "ner_then_llm",
) -> AssetAnnotations | None:
    """Run annotation enrichment for the single-asset MDL.

    Three modes (see `PipelineBehavior.concepts_source`):
      - 'ner_then_llm'  Foundry SeedFirstEntityLinker runs a deterministic
                        pre-pass against the tenant lexicon. Its candidates
                        ground the LLM call, which confirms/extends.
      - 'ner_only'      Skip the LLM. Emit only what the linker matched.
                        Cheap + deterministic; lowest recall on novel surfaces.
      - 'llm_only'      Legacy path — LLM with no NER grounding. Kept for
                        callers / backtests that need to compare.

    Returns None when:
      - The vocabulary is empty (nothing to anchor against).
      - 'ner_only' produced nothing AND there's no LLM to fall back on.
      - The LLM call failed / produced an empty annotation in modes that use it.
    """
    if not mdl.models:
        return None
    model = mdl.models[0]
    if not vocab.object_types and not vocab.causal_nodes and not vocab.key_areas:
        logger.info("Skipping annotation: vocabulary is empty for asset %s", model.rk)
        return None

    # ── NER pre-pass (used by ner_only + ner_then_llm) ─────────────────
    ner_concepts: list[Any] = []
    ner_key_areas: list[Any] = []
    ner_causal: list[Any] = []
    if concepts_source in ("ner_then_llm", "ner_only"):
        from ontology_pipeline.annotate_ner import (
            propose_causal_node_candidates,
            propose_concept_candidates,
            propose_key_area_candidates,
        )
        ner_concepts = propose_concept_candidates(model=model, vocab=vocab)
        ner_key_areas = propose_key_area_candidates(model=model, vocab=vocab)
        ner_causal = propose_causal_node_candidates(model=model, vocab=vocab)
        logger.debug(
            "NER pre-pass for %s: concepts=%d key_areas=%d causal=%d",
            model.rk, len(ner_concepts), len(ner_key_areas), len(ner_causal),
        )

    # ── NER-only short-circuit ─────────────────────────────────────────
    if concepts_source == "ner_only":
        concepts = [c.card_id for c in ner_concepts]
        key_areas = [c.card_id for c in ner_key_areas]
        causal_relations = [c.card_id for c in ner_causal]
        if not concepts and not key_areas and not causal_relations:
            return None
        avg_conf = (
            sum(c.confidence for c in ner_concepts + ner_key_areas + ner_causal)
            / max(1, len(ner_concepts) + len(ner_key_areas) + len(ner_causal))
        )
        rationale = (
            f"NER pre-pass matched {len(concepts)} concepts, "
            f"{len(key_areas)} key_areas, {len(causal_relations)} causal_nodes "
            f"against tenant lexicon."
        )
        model.concepts = concepts
        model.key_areas = key_areas
        model.causal_relations = causal_relations
        return AssetAnnotations(
            asset_rk=model.rk,
            concepts=concepts, key_areas=key_areas, causal_relations=causal_relations,
            confidence=avg_conf,
            rationale=rationale,
            source="ner_pre_pass",
            source_model=source_model,
            written_at=datetime.now(timezone.utc),
        )

    # ── LLM-required modes (ner_then_llm, llm_only) ─────────────────────
    if provider is None:
        # Without an LLM there's no way to finish these modes. Fall back to
        # whatever NER produced (if anything) instead of returning nothing —
        # gives the pipeline a graceful degradation path.
        if ner_concepts or ner_key_areas or ner_causal:
            logger.info(
                "No LLM provider for %s; emitting NER-only annotation as fallback",
                model.rk,
            )
            return enrich_annotations(
                mdl, vocab=vocab, provider=None,
                role=role, source_model=source_model,
                concepts_source="ner_only",
            )
        return None

    prompt = _build_annotation_prompt(
        model=model, vocab=vocab,
        ner_concepts=ner_concepts if concepts_source == "ner_then_llm" else [],
        ner_key_areas=ner_key_areas if concepts_source == "ner_then_llm" else [],
        ner_causal=ner_causal if concepts_source == "ner_then_llm" else [],
    )
    try:
        resp = llm_structured_transform(provider, role, prompt, _AnnotationResponse)
    except Exception as exc:
        logger.warning("LLM annotation failed for %s: %s", model.rk, exc)
        # Same graceful-degradation rule: if NER had results, emit those.
        if concepts_source == "ner_then_llm" and (ner_concepts or ner_key_areas or ner_causal):
            logger.info("LLM failed; falling back to NER candidates for %s", model.rk)
            return enrich_annotations(
                mdl, vocab=vocab, provider=None,
                role=role, source_model=source_model,
                concepts_source="ner_only",
            )
        return None

    # Post-filter: every concept/causal id must resolve; every key_area must be in vocab.
    concepts = [c for c in resp.concepts if c in vocab.object_type_ids]
    causal_relations = [c for c in resp.causal_relations if c in vocab.causal_node_ids]
    key_areas = [k for k in resp.key_areas if k in vocab.key_area_ids]

    if (
        not concepts
        and not key_areas
        and not causal_relations
        and not resp.rationale
    ):
        # LLM returned nothing meaningful; skip writing an empty annotation.
        return None

    # Mirror onto MDL model for export convenience
    model.concepts = concepts
    model.key_areas = key_areas
    model.causal_relations = causal_relations

    source = "ner_then_llm" if concepts_source == "ner_then_llm" else "llm_enrichment"
    return AssetAnnotations(
        asset_rk=model.rk,
        concepts=concepts,
        key_areas=key_areas,
        causal_relations=causal_relations,
        confidence=resp.confidence,
        rationale=resp.rationale,
        source=source,
        source_model=source_model,
        written_at=datetime.now(timezone.utc),
    )


def _build_annotation_prompt(
    *,
    model: MDLModel,
    vocab: SemanticVocab,
    ner_concepts: list[Any] | None = None,
    ner_key_areas: list[Any] | None = None,
    ner_causal: list[Any] | None = None,
) -> str:
    cols_block = "\n".join(
        f"  - {c.name} ({c.type}){' [PK]' if c.properties.is_primary_key else ''}"
        + (f" — {c.properties.description}" if c.properties.description else "")
        for c in model.columns
    )

    object_type_block = (
        "\n".join(c.to_prompt_line() for c in vocab.object_types)
        if vocab.object_types else "  (no object_type cards in tenant vocab)"
    )
    causal_node_block = (
        "\n".join(c.to_prompt_line() for c in vocab.causal_nodes)
        if vocab.causal_nodes else "  (no causal_node cards)"
    )
    key_areas_block = (
        "\n".join(k.to_prompt_line() for k in vocab.key_areas)
        if vocab.key_areas else "  (no key_areas vocabulary)"
    )

    ner_block = _format_ner_grounding(
        ner_concepts or [], ner_key_areas or [], ner_causal or [],
    )

    return f"""You annotate a data asset with its semantic identity. Output JSON only.

ASSET: {model.name}
RK: {model.rk}
KIND: {"view" if model.is_view else "table"}
DESCRIPTION: {model.description or "(none)"}

COLUMNS:
{cols_block}

CANDIDATE object_type CARDS (pick those this asset embodies, most-primary first):
{object_type_block}

CANDIDATE causal_node CARDS (pick those this asset feeds or affects; may be empty):
{causal_node_block}

KEY_AREAS VOCABULARY (pick strategic themes this asset serves):
{key_areas_block}
{ner_block}
Output JSON matching this exact schema:
{{
  "concepts":           [card_id, ...],     // 0–3 typical; ordered most-primary first
  "key_areas":          [key_area_id, ...], // 0–4 typical
  "causal_relations":   [card_id, ...],     // 0–5 typical; empty is allowed
  "confidence":         0.0,                // 0.0–1.0
  "rationale":          "one paragraph explaining your picks"
}}

Rules:
- Use ONLY ids that appear in the candidate lists above.
- If no candidate fits, return an empty array for that field.
- Junction/relationship tables represent the relationship itself; pick the most specific concept.
- Asset has the COLUMNS shown; do not invent columns when reasoning.
- If a deterministic-lexicon section is included above the schema block,
  treat its candidates as strong signals — confirm them, or override only
  with explicit justification in the rationale.
"""


def _format_ner_grounding(
    concepts: list[Any], key_areas: list[Any], causal: list[Any],
) -> str:
    """Compose an optional NER-grounding section for the prompt.

    Empty string when no NER candidates exist — keeps the prompt unchanged
    for callers in 'llm_only' mode.
    """
    if not (concepts or key_areas or causal):
        return ""
    blocks: list[str] = ["", "NER GROUNDING (deterministic lexicon matches in this asset):"]
    if concepts:
        blocks.append("  object_type:")
        for c in concepts:
            blocks.append(_format_candidate_line(c))
    if key_areas:
        blocks.append("  key_area:")
        for c in key_areas:
            blocks.append(_format_candidate_line(c))
    if causal:
        blocks.append("  causal_node:")
        for c in causal:
            blocks.append(_format_candidate_line(c))
    blocks.append("")
    return "\n".join(blocks)


def _format_candidate_line(cand: Any) -> str:
    """Render a `ConceptCandidate` as a prompt line.

    Tolerates the duck-typed shape (.card_id, .confidence, .evidence_text,
    .evidence_columns, .match_kind) so callers can pass any compatible object.
    """
    evid_cols = getattr(cand, "evidence_columns", None) or []
    evid_suffix = (
        f" via column(s): {', '.join(evid_cols)}" if evid_cols
        else f" via asset name"
    )
    return (
        f"    - {cand.card_id} "
        f"(confidence={cand.confidence:.2f}, match={cand.match_kind})"
        f"  ← matched on {cand.evidence_text!r}{evid_suffix}"
    )
