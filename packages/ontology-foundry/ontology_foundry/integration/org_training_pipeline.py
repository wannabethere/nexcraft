"""Integration runner: org-training scenario → tabular + document foundry outputs."""

from __future__ import annotations

from ontology_foundry.analysis.correlation_pipeline import emit_candidate_pair
from ontology_foundry.analysis.stats import profile_numeric_column
from ontology_foundry.document_pipeline import FoundryDocumentPipeline
from ontology_foundry.extractors import RuleBasedNerExtractor
from ontology_foundry.models import Document
from ontology_foundry.ner.pipeline import HybridNerConfig, HybridNerPipeline
from ontology_foundry.ner.stages import GlinerNerStage
from ontology_foundry.pipeline import OntologyFoundryPipeline
from ontology_foundry.retrieval import KeywordRetrievalAgent
from ontology_foundry.scenarios.org_training_dataset import (
    build_org_training_dataset,
    dataset_to_extractable_bundle,
)


def offline_document_ner_pipeline() -> HybridNerPipeline:
    """Hybrid NER without spaCy/GLiNER downloads — suitable for CI and demos."""
    ner = HybridNerPipeline(HybridNerConfig())
    ner.spacy_stage = None  # type: ignore[assignment]
    ner.fallback_stage = None  # type: ignore[assignment]
    ner.gliner_stage = GlinerNerStage(ner_labels=("quantitative_claim",), skip_model_load=True)
    return ner


def run_org_training_scenario_extractable() -> dict[str, object]:
    """
    End-to-end: synthetic relational sample → profiles / Tier-1 pair / optional Pearson /
    policy doc NER → keyword retrieval. Returns JSON-serializable-friendly dicts.
    """
    ds = build_org_training_dataset(n_employees=180, seed=7)

    profiles = {
        "tenure_months": profile_numeric_column("tenure_months", ds.tenure_months),
        "progress_percent": profile_numeric_column("progress_percent", ds.progress_percent),
        "is_overdue": profile_numeric_column("is_overdue", [float(x) for x in ds.is_overdue]),
    }

    tier1_pair = emit_candidate_pair(
        "tenure_months",
        "progress_percent",
        profiles=profiles,
        types={
            "tenure_months": "numeric",
            "progress_percent": "numeric",
        },
        seed_prior_boost=True,
    )

    correlations_raw: list[dict[str, object]] = []
    try:
        from ontology_foundry.analysis.correlation import pairwise_numeric_screen

        findings = pairwise_numeric_screen(
            {
                "tenure_months": ds.tenure_months,
                "progress_percent": ds.progress_percent,
            },
            method="pearson",
            alpha=0.05,
            min_effect=0.08,
        )
        correlations_raw = [f.model_dump() for f in findings]
    except ImportError:
        correlations_raw = []

    policy = Document(
        doc_id="policy-security-training-001",
        text=(
            "Per our SecurityAwarenessProgram, phishing simulation training reduces "
            "successful phishing attempts by ~40% when TenureMonths exceed twelve months. "
            "Mandatory completion aligns with TrainingCompletionRate targets and reduces OverdueRisk."
        ),
        metadata={"authority": "policy", "seed_concepts": ",".join(ds.seed_concepts)},
    )

    doc_pipe = FoundryDocumentPipeline.default()
    doc_pipe.ner = offline_document_ner_pipeline()
    doc_analysis = doc_pipe.analyze(policy)

    lex_pipe = OntologyFoundryPipeline(
        extractors=[RuleBasedNerExtractor()],
        retrieval_agents=[KeywordRetrievalAgent()],
    )
    lex_analysis = lex_pipe.analyze(
        policy,
        context_documents=[
            Document(
                doc_id="kb-snippet-1",
                text=(
                    "Studies link tenure to completion; SecurityAwarenessProgram metrics "
                    "track TrainingCompletionRate quarterly."
                ),
            )
        ],
        retrieval_query="tenure TrainingCompletionRate SecurityAwarenessProgram",
    )

    return {
        "dataset": dataset_to_extractable_bundle(ds),
        "tabular": {
            "profiles": {k: v.model_dump() for k, v in profiles.items()},
            "tier1_candidate_pair": tier1_pair.model_dump() if tier1_pair else None,
            "correlation_findings_pearson": correlations_raw,
        },
        "document": doc_analysis.model_dump(),
        "lexical_retrieval_pass": lex_analysis.model_dump(),
        "summary": {
            "n_employees": len(ds.employees),
            "span_count": sum(len(a.spans) for a in doc_analysis.span_artifacts),
            "causal_marker_entities": sum(1 for e in lex_analysis.entities if e.label == "causal_marker"),
        },
    }
