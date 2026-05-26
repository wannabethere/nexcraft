from ontology_foundry.document_pipeline import FoundryDocumentPipeline
from ontology_foundry.models import Document
from ontology_foundry.ner.pipeline import HybridNerConfig, HybridNerPipeline
from ontology_foundry.ner.stages import GlinerNerStage


def test_foundry_pipeline_end_to_end_offline() -> None:
    """Uses hybrid NER with GLiNER disabled so only rules + fallbacks run."""
    ner = HybridNerPipeline(HybridNerConfig())
    ner.spacy_stage = None  # type: ignore[assignment]
    ner.fallback_stage = None  # type: ignore[assignment]
    gl = GlinerNerStage(ner_labels=("quantitative_claim",), skip_model_load=True)
    ner.gliner_stage = gl

    pipe = FoundryDocumentPipeline.default()
    pipe.ner = ner

    doc = Document(
        doc_id="doc_47",
        text=(
            "Previous studies show phishing simulation training reduces "
            "successful phishing attempts by ~40%."
        ),
    )
    result = pipe.analyze(doc)
    labels = {e.label for e in result.entities}
    assert "causal_marker" in labels
    assert "quantitative_claim" in labels
    assert result.span_artifacts
