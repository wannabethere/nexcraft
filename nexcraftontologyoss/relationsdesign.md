Right — seeds, not constraints. The LLM gets a starter vocabulary so it prefers `has_payment` over inventing `payment_made`, but it can still propose novel predicates when the seeds don't fit. This is a much smaller change than it sounds: the seed list is just an extra section in the prompt and a prior on canonicalization.

## The shape

```
seeds (vocabulary, not a schema)
  │
  ▼
OpenLlmRelationStage  ──► edges tagged {predicate, is_novel}
  │
  ▼
canonicalizer (biased toward seed names)
  │
  ▼
induced schema + a "novel predicates" report
  │
  └─► high-support novels get promoted into the next seed pack
```

Seeds carry **no domain/range constraints** — that's still inferred. They're just predicate names with descriptions, so the design stays domain-generic. You ship a small "common" pack and load extra packs per project.

## `ontology_foundry/relations/seeds.py` — new

```python
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RelationSeed:
    """A predicate hint. Not a constraint — the LLM may still propose novel
    predicates when none of the seeds fit."""
    predicate: str
    description: str
    examples: tuple[str, ...] = ()
    # Soft type hints — mentioned in the prompt, not enforced.
    preferred_domain: tuple[str, ...] = ()
    preferred_range: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeedPack:
    name: str
    seeds: tuple[RelationSeed, ...]

    def merge(self, other: SeedPack) -> SeedPack:
        seen = {s.predicate for s in self.seeds}
        extra = tuple(s for s in other.seeds if s.predicate not in seen)
        return SeedPack(name=f"{self.name}+{other.name}", seeds=self.seeds + extra)


# Cross-domain starters. Useful in almost any corpus.
COMMON_SEEDS = SeedPack("common", seeds=(
    RelationSeed("has_part",      "Whole-to-part composition.",     ("a contract has parts: subscription, meter")),
    RelationSeed("member_of",     "Element belongs to a group.",    ("employee member_of department",)),
    RelationSeed("located_in",    "Spatial containment.",           ("office located_in city",)),
    RelationSeed("occurred_at",   "Temporal anchor of an event.",   ("payment occurred_at 2026-04-30",)),
    RelationSeed("caused_by",     "Causal antecedent.",             ("outage caused_by misconfiguration",)),
    RelationSeed("mentions",      "Document references an entity.", ("report mentions Acme Corp",)),
    RelationSeed("succeeded_by",  "Ordered sequence.",              ("contract v1 succeeded_by v2",)),
    RelationSeed("attributed_to", "Authorship / responsibility.",   ("claim attributed_to underwriter",)),
))

# Domain pack — same shape, lives next to it. Users add more without touching core.
BILLING_SEEDS = SeedPack("billing", seeds=(
    RelationSeed("has_contract",     "Customer holds a contract."),
    RelationSeed("has_subscription", "Contract grants a subscription."),
    RelationSeed("has_meter",        "Contract is served by a meter."),
    RelationSeed("has_reading",      "Meter produced a reading."),
    RelationSeed("has_invoice",      "Contract was invoiced."),
    RelationSeed("has_payment",      "Invoice was paid."),
    RelationSeed("made_call",        "Customer initiated a service call."),
    RelationSeed("filed_claim",      "Customer filed a claim."),
    RelationSeed("has_interaction",  "Customer had a logged interaction."),
))
```

## Update `OpenLlmRelationStage` — seeded prompt, novelty flag

Two changes: the prompt lists seeds with descriptions, and the LLM is asked to flag novel predicates. The model parses that flag through.

```python
@dataclass
class OpenLlmRelationStage:
    provider: LlmProvider
    seeds: SeedPack = field(default_factory=lambda: COMMON_SEEDS)
    name: str = "seeded-llm-relations"
    min_confidence: float = 0.5
    allow_novel: bool = True

    def extract(self, chunk, spans, claims=None):
        if len(spans) < 2:
            return []
        raw = self.provider.complete_json(
            self._build_prompt(chunk.text, spans, claims or [])
        )
        proposals = raw.get("relations", []) if isinstance(raw, dict) else []

        seed_names = {s.predicate for s in self.seeds.seeds}
        out: list[RelationArtifact] = []
        for p in proposals:
            try:
                i, j = int(p["subject_idx"]), int(p["object_idx"])
                pred = _normalize_surface(str(p["predicate"]))
                conf = float(p.get("confidence", 0.0))
            except (KeyError, TypeError, ValueError):
                continue
            if i == j or not (0 <= i < len(spans) and 0 <= j < len(spans)):
                continue
            if conf < self.min_confidence or not pred:
                continue
            is_novel = pred not in seed_names
            if is_novel and not self.allow_novel:
                continue
            subj, obj = spans[i], spans[j]
            out.append(RelationArtifact(
                subject_ref=_ref(subj),
                predicate=pred,
                object_ref=_ref(obj),
                chunk_id=chunk.metadata.chunk_id,
                confidence=conf,
                subject_span_idx=i,
                object_span_idx=j,
                source=f"{self.name}:{'novel' if is_novel else 'seeded'}",
                evidence_text=p.get("evidence"),
            ))
        return out

    def _build_prompt(self, text, spans, claims):
        seed_block = "\n".join(
            f"- {s.predicate}: {s.description}"
            + (f"  e.g. {s.examples[0]}" if s.examples else "")
            for s in self.seeds.seeds
        )
        span_block = "\n".join(
            f"[{i}] {s.span_type}: {s.text!r}"
            + (f" (anchor={s.seed_anchor})" if s.seed_anchor else "")
            for i, s in enumerate(spans)
        )
        claim_block = (
            "\n".join(f"- ({c.claim_type}) {c.text}" for c in claims) or "(none)"
        )
        return (
            "Extract typed relations between the entities below.\n\n"
            "PREFERRED predicates (use these exact names when applicable):\n"
            f"{seed_block}\n\n"
            "If none of the preferred predicates fits, you may invent a new "
            "snake_case predicate name. Only emit a relation if the text or a "
            "claim directly supports it.\n\n"
            'Return JSON: {"relations": [{"subject_idx": int, "predicate": str, '
            '"object_idx": int, "confidence": float, "evidence": str}]}\n\n'
            f"Entities:\n{span_block}\n\n"
            f"Claims:\n{claim_block}\n\n"
            f"Text:\n{text}"
        )
```

## Bias the canonicalizer toward seeds

One-line change in `induction.py` — pass seed names through and let the LLM know they're the preferred canonical forms.

```python
def _canonicalize_predicates(
    surfaces: list[str],
    seeds: SeedPack,
    provider: LlmProvider,
) -> dict[str, str]:
    seed_names = [s.predicate for s in seeds.seeds]
    prompt = (
        "Group the predicate surfaces into clusters of synonymous meaning. "
        "When a cluster matches one of the PREFERRED canonical names below, "
        "use that name. Otherwise pick a snake_case canonical.\n\n"
        f"Preferred canonical names:\n{chr(10).join('- ' + n for n in seed_names)}\n\n"
        f"Surfaces:\n{chr(10).join('- ' + s for s in surfaces)}\n\n"
        'Return JSON: {"clusters": [{"canonical": str, "members": [str, ...]}]}'
    )
    raw = provider.complete_json(prompt)
    out: dict[str, str] = {}
    for cluster in raw.get("clusters", []) if isinstance(raw, dict) else []:
        canonical = str(cluster.get("canonical", "")).strip().lower()
        if not canonical:
            continue
        for m in cluster.get("members", []):
            out[str(m)] = canonical
    for s in surfaces:
        out.setdefault(s, s)
    return out
```

## Promote novels — close the loop

After induction you have `InducedPredicate` entries with `support`, `avg_confidence`, and a flag (cheap to add) for whether the canonical name is already in the seed pack. A small helper writes the promotion candidates so you can review them and append to a seed pack:

```python
def novel_promotion_candidates(
    induced: list[InducedPredicate],
    seeds: SeedPack,
    min_support: int = 10,
    min_confidence: float = 0.7,
) -> list[RelationSeed]:
    seed_names = {s.predicate for s in seeds.seeds}
    out: list[RelationSeed] = []
    for p in induced:
        if p.canonical in seed_names:
            continue
        if p.support < min_support or p.avg_confidence < min_confidence:
            continue
        out.append(RelationSeed(
            predicate=p.canonical,
            description=f"Auto-promoted: observed {p.support}× "
                        f"(typical {p.dominant_domain() or '?'} → "
                        f"{p.dominant_range() or '?'}).",
        ))
    return out
```

This is the "ontology grows itself" pattern: start with `COMMON_SEEDS` only, run on a corpus, look at the promotion candidates, accept what's real, reject what's noise, and the next run uses a stronger seed pack. After a few iterations the seed pack *is* your domain ontology.

## Why this is the right middle

- **Cuts predicate variance fast.** Even a 5–10 seed pack collapses the long tail dramatically because the LLM defaults to known names rather than rolling its own.
- **Still domain-agnostic.** The pipeline doesn't import `BILLING_SEEDS`. It imports a `SeedPack` parameter. Same code processes any domain by passing a different pack.
- **Honest about novelty.** Every artifact records `seeded` vs `novel` in `source`. Downstream consumers can filter on it; reviewers can audit it.
- **Cheap to evolve.** Promotion is just appending entries to a seed pack file. No code change, no migration.
- **Schema is still induced.** `domain` and `range` come from observed types — seeds don't dictate them. So a `has_contract` seed used on a non-billing corpus would still get its true domain/range from the data.

## What I'd not do

- **Don't make seeds enforce types.** The moment you do, you've reinvented the schema-first design and lost the genericity. If you later want enforcement on a specific project, induce the schema once and switch *that project* to schema-constrained mode. Keep the seed-based stage as the default.
- **Don't auto-promote without a review surface.** Auto-promotion drifts. The candidate list is the right output; promotion to a seed pack is a human (or LLM-with-approval) action.

Ready to write this into the repo when you give the word. Suggested layout:
- `ontology_foundry/relations/seeds.py` (new)
- `ontology_foundry/relations/stages.py` (new — seeded open extractor)
- `ontology_foundry/relations/induction.py` (new — seed-biased canonicalization + promotion)
- `ontology_foundry/relations/pipeline.py` (new — composer)
- `ontology_foundry/relations/turtle.py` (new — TBox + ABox serializers)
- `ontology_foundry/models.py` — add `RelationArtifact`
- `tests/test_relations.py` — uses a stub `LlmProvider` returning canned JSON; deterministic.