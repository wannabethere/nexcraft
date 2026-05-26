from __future__ import annotations

import json

from ontology_foundry import (
    ChunkMetadata,
    DocumentChunk,
    EntitySpan,
    EntitySpanArtifact,
    ModelRole,
    RelationPipeline,
    SeededLlmRelationStage,
    SeedPack,
    StaticJsonProvider,
    StubRelationStage,
    induce_schema,
    novel_promotion_candidates,
)
from ontology_foundry.relations.seeds import RelationSeed


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _chunk(chunk_id: str, text: str) -> DocumentChunk:
    return DocumentChunk(
        metadata=ChunkMetadata(chunk_id=chunk_id, parent_doc_id="doc-1"),
        text=text,
    )


def _span(text: str, span_type: str, start: int, end: int, anchor: str | None) -> EntitySpan:
    return EntitySpan(
        text=text,
        span_type=span_type,
        source_model="test",
        char_start=start,
        char_end=end,
        confidence=1.0,
        seed_anchor=anchor,
    )


def _billing_seeds() -> SeedPack:
    return SeedPack(
        name="billing-test",
        seeds=(
            RelationSeed(predicate="has_contract", description="Customer holds a contract."),
            RelationSeed(predicate="has_invoice", description="Contract was invoiced."),
            RelationSeed(predicate="has_payment", description="Invoice was paid."),
        ),
    )


# ---------------------------------------------------------------------------
# SeededLlmRelationStage
# ---------------------------------------------------------------------------


def test_seeded_llm_stage_emits_seeded_and_novel_edges() -> None:
    chunk = _chunk("c1", "Acme signed contract K-22; invoice I-91 was paid via P-300.")
    spans = [
        _span("Acme", "Customer", 0, 4, "cust:Acme"),
        _span("K-22", "Contract", 22, 26, "ctr:K-22"),
        _span("I-91", "Invoice", 36, 40, "inv:I-91"),
        _span("P-300", "Payment", 53, 58, "pay:P-300"),
    ]
    canned = json.dumps(
        {
            "relations": [
                {"subject_idx": 0, "predicate": "has_contract", "object_idx": 1,
                 "confidence": 0.95, "evidence": "Acme signed contract K-22"},
                {"subject_idx": 1, "predicate": "has_invoice", "object_idx": 2,
                 "confidence": 0.9, "evidence": "invoice I-91"},
                {"subject_idx": 2, "predicate": "paid_via", "object_idx": 3,
                 "confidence": 0.8, "evidence": "paid via P-300"},
                {"subject_idx": 0, "predicate": "ignored", "object_idx": 1,
                 "confidence": 0.2},
                {"subject_idx": 99, "predicate": "has_payment", "object_idx": 0,
                 "confidence": 0.9},
            ]
        }
    )
    provider = StaticJsonProvider({ModelRole.RELATION_EXTRACTOR: canned})
    stage = SeededLlmRelationStage(provider=provider, seeds=_billing_seeds())

    edges = stage.extract(chunk, spans)
    by_pred = {e.predicate: e for e in edges}

    assert set(by_pred) == {"has_contract", "has_invoice", "paid_via"}
    assert by_pred["has_contract"].source.endswith(":seeded")
    assert by_pred["paid_via"].source.endswith(":novel")
    assert by_pred["has_contract"].subject_ref == "cust:Acme"
    assert by_pred["has_invoice"].object_type == "Invoice"
    assert by_pred["paid_via"].evidence_text == "paid via P-300"


def test_seeded_stage_disallowing_novel_keeps_only_seeded() -> None:
    chunk = _chunk("c1", "x")
    spans = [
        _span("Acme", "Customer", 0, 4, "cust:Acme"),
        _span("K-22", "Contract", 5, 9, "ctr:K-22"),
    ]
    canned = json.dumps(
        {
            "relations": [
                {"subject_idx": 0, "predicate": "has_contract", "object_idx": 1, "confidence": 0.9},
                {"subject_idx": 0, "predicate": "novel_one", "object_idx": 1, "confidence": 0.9},
            ]
        }
    )
    stage = SeededLlmRelationStage(
        provider=StaticJsonProvider({ModelRole.RELATION_EXTRACTOR: canned}),
        seeds=_billing_seeds(),
        allow_novel=False,
    )
    edges = stage.extract(chunk, spans)
    assert [e.predicate for e in edges] == ["has_contract"]


# ---------------------------------------------------------------------------
# StubRelationStage + pipeline dedupe
# ---------------------------------------------------------------------------


def test_stub_stage_emits_recipes_and_pipeline_dedupes() -> None:
    chunk = _chunk("c1", "irrelevant")
    spans = [
        _span("Acme", "Customer", 0, 4, "cust:Acme"),
        _span("K-22", "Contract", 5, 9, "ctr:K-22"),
    ]
    art = EntitySpanArtifact(chunk_id="c1", spans=spans)

    low_conf = StubRelationStage(
        edges_by_chunk={"c1": ((0, "has_contract", 1, 0.4),)},
        name="low",
    )
    high_conf = StubRelationStage(
        edges_by_chunk={"c1": ((0, "has_contract", 1, 0.9),)},
        name="high",
    )
    pipe = RelationPipeline(stages=(low_conf, high_conf))

    edges = pipe.extract_chunk(chunk, art)
    assert len(edges) == 1
    assert edges[0].confidence == 0.9
    assert edges[0].source == "high"


# ---------------------------------------------------------------------------
# induction
# ---------------------------------------------------------------------------


def test_induce_schema_canonicalizes_and_aggregates_types() -> None:
    edges = [
        # Two synonyms for has_payment, plus enough support to make the cut.
        *_n_edges(3, predicate="paid", subj=("inv:1", "Invoice"), obj=("pay:1", "Payment")),
        *_n_edges(2, predicate="payment_for", subj=("inv:2", "Invoice"), obj=("pay:2", "Payment")),
        # has_contract directly.
        *_n_edges(4, predicate="has_contract", subj=("cust:a", "Customer"), obj=("ctr:1", "Contract")),
        # Low-support noise — drops below min_support and is excluded.
        *_n_edges(1, predicate="weird_predicate", subj=("x:1", "X"), obj=("y:1", "Y")),
    ]
    canonicalization = json.dumps(
        {
            "clusters": [
                {"canonical": "has_payment", "members": ["paid", "payment_for"]},
                {"canonical": "has_contract", "members": ["has_contract"]},
            ]
        }
    )
    provider = StaticJsonProvider({ModelRole.PREDICATE_CANONICALIZER: canonicalization})
    seeds = _billing_seeds()

    schema, induced = induce_schema(edges, provider, seeds, min_support=3)

    by_pred = {t.predicate: t for t in schema.types}
    assert set(by_pred) == {"has_payment", "has_contract"}
    assert by_pred["has_payment"].domain == "Invoice"
    assert by_pred["has_payment"].range == "Payment"
    assert by_pred["has_contract"].domain == "Customer"
    assert by_pred["has_contract"].range == "Contract"

    has_payment = next(p for p in induced if p.canonical == "has_payment")
    assert set(has_payment.surfaces) == {"paid", "payment_for"}
    assert has_payment.support == 5


def test_novel_promotion_candidates_filters_seeded_and_low_support() -> None:
    from collections import Counter

    from ontology_foundry.relations.induction import InducedPredicate

    seeds = _billing_seeds()
    induced = [
        InducedPredicate(
            canonical="has_contract",  # already a seed — skipped
            surfaces=("has_contract",),
            domain_counts=Counter({"Customer": 12}),
            range_counts=Counter({"Contract": 12}),
            support=12,
            avg_confidence=0.9,
        ),
        InducedPredicate(
            canonical="paid_via",  # novel and well-supported — promote
            surfaces=("paid_via", "settled_via"),
            domain_counts=Counter({"Invoice": 11}),
            range_counts=Counter({"Payment": 11}),
            support=11,
            avg_confidence=0.85,
        ),
        InducedPredicate(
            canonical="weak",  # below min_support
            surfaces=("weak",),
            domain_counts=Counter({"A": 3}),
            range_counts=Counter({"B": 3}),
            support=3,
            avg_confidence=0.9,
        ),
    ]

    candidates = novel_promotion_candidates(induced, seeds, min_support=10, min_confidence=0.7)
    assert [c.predicate for c in candidates] == ["paid_via"]
    assert candidates[0].preferred_domain == ("Invoice",)
    assert candidates[0].preferred_range == ("Payment",)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_edge(
    subject_ref: str,
    predicate: str,
    object_ref: str,
    subject_type: str,
    object_type: str,
    chunk_id: str,
    confidence: float,
):
    from ontology_foundry import RelationArtifact

    return RelationArtifact(
        subject_ref=subject_ref,
        predicate=predicate,
        object_ref=object_ref,
        subject_type=subject_type,
        object_type=object_type,
        chunk_id=chunk_id,
        confidence=confidence,
    )


def _n_edges(n: int, *, predicate: str, subj: tuple[str, str], obj: tuple[str, str]):
    return [
        _make_edge(
            subject_ref=subj[0],
            predicate=predicate,
            object_ref=obj[0],
            subject_type=subj[1],
            object_type=obj[1],
            chunk_id=f"c-{i}",
            confidence=0.8,
        )
        for i in range(n)
    ]
