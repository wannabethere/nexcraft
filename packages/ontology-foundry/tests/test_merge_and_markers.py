from ontology_foundry.models import EntitySpan
from ontology_foundry.ner.merge import merge_entity_spans
from ontology_foundry.ner.stages import CausalMarkerStage


def test_causal_marker_finds_reduces() -> None:
    text = "Phishing simulation training reduces successful phishing attempts by ~40%."
    stage = CausalMarkerStage()
    spans = stage.extract(text)
    texts = {s.text.lower() for s in spans}
    assert "reduces" in texts
    assert any(s.span_type == "causal_marker" for s in spans)


def test_merge_prefers_causal_marker_over_generic() -> None:
    spans = [
        EntitySpan(
            text="reduces risk",
            span_type="PROPER_NOUN",
            source_model="bad",
            char_start=10,
            char_end=22,
            confidence=0.9,
        ),
        EntitySpan(
            text="reduces",
            span_type="causal_marker",
            source_model="rule_based",
            char_start=10,
            char_end=17,
            confidence=1.0,
        ),
    ]
    merged = merge_entity_spans(spans)
    assert len(merged) == 1
    assert merged[0].span_type == "causal_marker"


def test_quantitative_fallback_pattern() -> None:
    from ontology_foundry.ner.stages import GlinerNerStage

    gl = GlinerNerStage(ner_labels=("quantitative_claim",), skip_model_load=True)
    spans = gl.extract("improved completion by ~40% quarter over quarter")
    assert spans
    assert any(s.span_type == "quantitative_claim" for s in spans)
