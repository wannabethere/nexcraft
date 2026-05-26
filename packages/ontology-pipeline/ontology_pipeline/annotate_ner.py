"""NER pre-pass for the concepts/key_areas annotation stage.

Wires `ontology_foundry.linking.SeedFirstEntityLinker` (and `normalize_for_exact_lookup`)
into a deterministic, lexicon-based first pass over an asset's surface text
(table name, column names, column descriptions). The output — a list of
`ConceptCandidate` — feeds the LLM call as grounding context when
`concepts_source='ner_then_llm'`, OR replaces it entirely when
`concepts_source='ner_only'`.

Why a foundry hook, not bespoke string matching:

  - The linker (`SeedFirstEntityLinker`) is the canonical entry point in
    ontology-foundry for mapping surface text → seed-concept anchors.
  - Sharing the matcher means: the same lexicon definition feeds annotation,
    causal-card resolution, and future document-derived enrichment paths
    without drift.
  - `normalize_for_exact_lookup` is the contract — lowercase + collapse
    whitespace — so the seed dict and the runtime lookup agree.

What we DON'T do here:

  - Run full `HybridNerPipeline` (spaCy + GLiNER). Table/column names aren't
    sentences; the model overhead doesn't earn its keep at this stage. The
    pipeline already does well on its own ground.
  - Build a lexicon from anything other than the tenant card vocab. NER is a
    closed-world match against authored cards. The LLM is the open-world step.

Shape contract:

    propose_concept_candidates(model, vocab) -> list[ConceptCandidate]
    propose_key_area_candidates(model, vocab) -> list[ConceptCandidate]

Both return ordered candidates (most-supported first). When the surface text
of an asset's name or one of its columns normalizes to a card_id / title /
alias, that card becomes a candidate. Multiple matches on the same card_id
collapse into one row with the supporting evidence preserved.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ontology_foundry.linking.entity_linker import (
    SeedFirstEntityLinker,
    normalize_for_exact_lookup,
)

if TYPE_CHECKING:  # pragma: no cover
    from ontology_pipeline.annotate import KeyAreaEntry, SemanticVocab
    from ontology_pipeline.models import MDLModel

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# Output shape
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConceptCandidate:
    """One card-id proposal from the NER/linker pre-pass.

    Always references a card that exists in the tenant vocab — the linker only
    emits anchors it found in the seed dict.

    Fields:
      - `card_id`: the matched card id (linker output).
      - `kind`: `object_type` | `causal_node` | `key_area` — for routing.
      - `confidence`: a heuristic 0..1 score reflecting match strength
        (1.0 = exact name match, 0.85 = alias, 0.7 = token, 0.5 = description token).
      - `evidence_text`: the surface text that triggered the match.
      - `evidence_columns`: column names whose surface or description triggered
        the match. Empty list when the match came from the table name itself.
      - `match_kind`: `name` | `alias` | `token` | `description` — tells the
        LLM prompt why this candidate is in scope.
    """
    card_id: str
    kind: str
    confidence: float
    evidence_text: str
    evidence_columns: list[str] = field(default_factory=list)
    match_kind: str = "name"


# ───────────────────────────────────────────────────────────────────────────
# Lexicon construction
# ───────────────────────────────────────────────────────────────────────────


def build_lexicon(items: list[Any]) -> dict[str, str]:
    """Build `{normalized_surface: card_id}` from a list of CardSummary-like rows.

    Accepts anything with `.id` and `.title` attrs (the SemanticVocab card rows
    OR the KeyAreaEntry rows — both work). For each item we register:
      - The card id (verbatim)
      - The card id with `_`/`-` flipped to spaces (so `compliance_gap`
        normalizes alongside `compliance gap`)
      - The title, if present
      - Each alias under `frontmatter['aliases']`, if present on the item

    Conflict policy: first writer wins. Authoring tools should keep aliases
    unique across the lexicon; conflicts only matter when two distinct cards
    declare the same alias, which is a vocabulary bug worth a warning.
    """
    lex: dict[str, str] = {}
    for item in items:
        card_id = getattr(item, "id", None)
        if not card_id:
            continue
        # Core surfaces
        for surface in _surfaces_for(item):
            norm = normalize_for_exact_lookup(surface)
            if not norm:
                continue
            if norm in lex and lex[norm] != card_id:
                logger.warning(
                    "Lexicon conflict on surface %r: %s already mapped, ignoring %s",
                    norm, lex[norm], card_id,
                )
                continue
            lex.setdefault(norm, card_id)
    return lex


def _surfaces_for(item: Any) -> list[str]:
    out: list[str] = []
    card_id = getattr(item, "id", None)
    if not card_id:
        return out
    out.append(card_id)
    out.append(_id_to_phrase(card_id))
    title = getattr(item, "title", None)
    if title:
        out.append(str(title))
    # CardSummary doesn't carry aliases directly — they're in the body excerpt
    # for the LLM. For now, only id + title + id-as-phrase populate the lexicon.
    # If the DAO surface adds aliases later, they slot in here.
    aliases = getattr(item, "aliases", None) or []
    out.extend(str(a) for a in aliases)
    return out


_ID_SPLIT_RE = re.compile(r"[_\-]+")


def _id_to_phrase(card_id: str) -> str:
    """`compliance_gap` → `compliance gap`. The linker normalises whitespace."""
    return _ID_SPLIT_RE.sub(" ", card_id)


# ───────────────────────────────────────────────────────────────────────────
# Asset → candidate proposals
# ───────────────────────────────────────────────────────────────────────────


def propose_concept_candidates(
    *, model: "MDLModel", vocab: "SemanticVocab",
) -> list[ConceptCandidate]:
    """NER pre-pass against the `object_type` cards in `vocab`.

    Returns a list of `ConceptCandidate` (kind='object_type') deduplicated by
    card_id, ordered by descending confidence. When two surfaces in the asset
    map to the same card, the higher-confidence match wins and the lower one
    is folded into `evidence_columns`.
    """
    lexicon = build_lexicon(vocab.object_types)
    return _run_linker(
        model=model, lexicon=lexicon, kind="object_type",
    )


def propose_key_area_candidates(
    *, model: "MDLModel", vocab: "SemanticVocab",
) -> list[ConceptCandidate]:
    """NER pre-pass against the `key_area` vocab. Same algorithm, separate kind."""
    lexicon = build_lexicon(vocab.key_areas)
    return _run_linker(
        model=model, lexicon=lexicon, kind="key_area",
    )


def propose_causal_node_candidates(
    *, model: "MDLModel", vocab: "SemanticVocab",
) -> list[ConceptCandidate]:
    """NER pre-pass against the `causal_node` cards. Same algorithm, separate kind."""
    lexicon = build_lexicon(vocab.causal_nodes)
    return _run_linker(
        model=model, lexicon=lexicon, kind="causal_node",
    )


def _run_linker(
    *, model: "MDLModel", lexicon: dict[str, str], kind: str,
) -> list[ConceptCandidate]:
    if not lexicon:
        return []

    linker = SeedFirstEntityLinker(concepts_by_normalized_surface=lexicon)
    aggregator: dict[str, ConceptCandidate] = {}

    # Build the surface set to test. Each entry is (text, confidence, match_kind, evidence_col).
    surfaces = list(_surfaces_for_asset(model))
    for text, confidence, match_kind, evidence_col in surfaces:
        anchor = _link_one(linker, text)
        if anchor is None:
            continue
        existing = aggregator.get(anchor)
        if existing is None:
            aggregator[anchor] = ConceptCandidate(
                card_id=anchor, kind=kind, confidence=confidence,
                evidence_text=text,
                evidence_columns=[evidence_col] if evidence_col else [],
                match_kind=match_kind,
            )
            continue
        # Already have this card — fold in additional evidence.
        merged_cols = list(existing.evidence_columns)
        if evidence_col and evidence_col not in merged_cols:
            merged_cols.append(evidence_col)
        # Prefer the highest-confidence match for evidence_text + match_kind.
        if confidence > existing.confidence:
            aggregator[anchor] = ConceptCandidate(
                card_id=anchor, kind=kind, confidence=confidence,
                evidence_text=text, evidence_columns=merged_cols,
                match_kind=match_kind,
            )
        else:
            aggregator[anchor] = ConceptCandidate(
                card_id=existing.card_id, kind=existing.kind,
                confidence=existing.confidence,
                evidence_text=existing.evidence_text,
                evidence_columns=merged_cols,
                match_kind=existing.match_kind,
            )
    return sorted(aggregator.values(), key=lambda c: (-c.confidence, c.card_id))


# ───────────────────────────────────────────────────────────────────────────
# Surface enumeration — what we feed into the linker
# ───────────────────────────────────────────────────────────────────────────


# Tokens we never treat as concept hints — they appear on virtually every
# table and would create false positives if the lexicon happened to contain
# them.
_STOPWORD_TOKENS = frozenset({
    "id", "ids", "pk", "key", "fk",
    "name", "date", "time", "ts", "timestamp",
    "code", "status", "type", "kind", "value",
    "amount", "qty", "count",
    "created", "updated", "deleted", "modified",
    "by", "at", "on", "of", "the", "and", "or",
    "is", "has", "in",
})


def _surfaces_for_asset(model: "MDLModel"):
    """Yield `(text, confidence, match_kind, evidence_column)` tuples for one model.

    Surface tiers (highest confidence first):
      1.00 — full asset name (verbatim + `_→ ` phrase form)
      0.85 — full column name (verbatim + phrase form), skipping bare stopwords
      0.75 — consecutive bigram of column-name tokens (catches multi-word card ids)
      0.70 — single non-stopword token from asset/column names
      0.50 — non-stopword tokens (≥3 chars) from column descriptions

    Asset-name tokens are emitted too — they catch cards whose id appears as a
    fragment of a compound table name (e.g., `hipaa_audit_log` → `hipaa`).
    """
    # ── Whole table name (highest confidence) ──────────────────────────
    name_phrase = _id_to_phrase(model.name)
    yield (model.name, 1.0, "name", "")
    if name_phrase != model.name:
        yield (name_phrase, 1.0, "name", "")
    # Asset-name tokens — catch fragment matches on compound names
    for tok in _split_tokens(model.name):
        if tok in _STOPWORD_TOKENS:
            continue
        yield (tok, 0.70, "token", "")
    # Bigrams of asset-name tokens — catch multi-word card ids on the table
    for bigram in _consecutive_bigrams(model.name):
        if bigram:
            yield (bigram, 0.75, "alias", "")

    # ── Per-column surfaces ───────────────────────────────────────────
    for col in model.columns:
        # Skip columns whose entire name is a stopword (id, name, status, …).
        # The token-level loop would still emit those, but the alias-tier
        # match would fire on the bare surface and pollute candidates.
        col_norm = col.name.strip().lower()
        if col_norm not in _STOPWORD_TOKENS:
            col_phrase = _id_to_phrase(col.name)
            yield (col.name, 0.85, "alias", col.name)
            if col_phrase != col.name:
                yield (col_phrase, 0.85, "alias", col.name)
            for bigram in _consecutive_bigrams(col.name):
                if bigram:
                    yield (bigram, 0.75, "alias", col.name)
        for tok in _split_tokens(col.name):
            if tok in _STOPWORD_TOKENS:
                continue
            yield (tok, 0.70, "token", col.name)
        desc = (col.properties.description or "") if col.properties else ""
        if desc:
            for tok in _split_tokens(desc):
                if tok in _STOPWORD_TOKENS or len(tok) < 3:
                    continue
                yield (tok, 0.50, "description", col.name)


def _consecutive_bigrams(text: str) -> list[str]:
    """Yield space-joined consecutive token pairs from a compound id.

    `compliance_gap_days` → ['compliance gap', 'gap days'].
    Stopword bigrams are filtered to keep noise down.
    """
    tokens = _split_tokens(text)
    out: list[str] = []
    for i in range(len(tokens) - 1):
        a, b = tokens[i], tokens[i + 1]
        if a in _STOPWORD_TOKENS or b in _STOPWORD_TOKENS:
            continue
        out.append(f"{a} {b}")
    return out


_TOKEN_SPLIT_RE = re.compile(r"[\s_\-,.;:/()\[\]{}]+")


def _split_tokens(text: str) -> list[str]:
    return [t for t in (s.strip().lower() for s in _TOKEN_SPLIT_RE.split(text)) if t]


def _link_one(linker: SeedFirstEntityLinker, text: str) -> str | None:
    """Run the linker on a synthetic single-token span. Returns the matched
    card_id or None.

    We construct a minimal EntitySpan rather than going through the full NER
    pipeline because tabular metadata is short and structured — the value of
    foundry's `link()` here is the canonical lookup + normalization, not the
    NER step itself.
    """
    from ontology_foundry.models import EntitySpan
    span = EntitySpan(
        text=text,
        span_type="surface",  # generic; linker ignores type
        source_model="metadata_surface",
        char_start=0,
        char_end=len(text),
        confidence=1.0,
    )
    linked = linker.link(span)
    return linked.seed_anchor
