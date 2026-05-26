from ontology_foundry.linking.entity_linker import SeedFirstEntityLinker
from ontology_foundry.models import EntitySpan


def test_seed_exact_link_sets_anchor() -> None:
    linker = SeedFirstEntityLinker(
        concepts_by_normalized_surface={"phishing simulation training": "PhishingTrainingProgram"}
    )
    span = EntitySpan(
        text="Phishing simulation training",
        span_type="concept",
        source_model="test",
        char_start=0,
        char_end=28,
        confidence=0.9,
    )
    linked = linker.link(span)
    assert linked.seed_anchor == "PhishingTrainingProgram"
