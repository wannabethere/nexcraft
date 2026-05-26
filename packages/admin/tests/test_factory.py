from nexcraft_admin import ExtractorConfig, OntologyFoundryConfig, RetrievalAgentConfig, build_foundry_pipeline
from ontology_foundry import Document


def test_build_foundry_pipeline_from_config() -> None:
    config = OntologyFoundryConfig(
        extractors=[ExtractorConfig(kind="rule_based_ner", enabled=True)],
        retrieval_agents=[RetrievalAgentConfig(kind="keyword", enabled=True)],
    )

    pipeline = build_foundry_pipeline(config)
    result = pipeline.analyze(
        Document(doc_id="doc-1", text="Bob moved to Denver; leadership training reduces attrition risk."),
        context_documents=[Document(doc_id="ctx-1", text="Denver is growing rapidly.")],
        retrieval_query="Denver",
    )

    assert result.entities
    assert result.retrieval_hits
