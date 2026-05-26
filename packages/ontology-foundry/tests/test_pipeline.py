from ontology_foundry import Document, KeywordRetrievalAgent, OntologyFoundryPipeline, RuleBasedNerExtractor


def test_pipeline_returns_entities_and_hits() -> None:
    pipeline = OntologyFoundryPipeline(
        extractors=[RuleBasedNerExtractor()],
        retrieval_agents=[KeywordRetrievalAgent()],
    )
    doc = Document(doc_id="doc-1", text="Alice works at Acme Corporation; training reduces risk.")
    ctx = [Document(doc_id="ctx-1", text="Acme Corporation has offices in Seattle.")]

    result = pipeline.analyze(doc, context_documents=ctx, retrieval_query="Acme Seattle")

    assert result.document_id == "doc-1"
    assert result.entities
    assert result.retrieval_hits
