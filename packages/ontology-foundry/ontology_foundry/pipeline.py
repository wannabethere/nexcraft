from __future__ import annotations

from ontology_foundry.extractors import EntityExtractor
from ontology_foundry.models import AnalysisResult, Document, Entity, RetrievalHit
from ontology_foundry.retrieval import RetrievalAgent


class OntologyFoundryPipeline:
    def __init__(
        self,
        extractors: list[EntityExtractor],
        retrieval_agents: list[RetrievalAgent] | None = None,
    ) -> None:
        self.extractors = extractors
        self.retrieval_agents = retrieval_agents or []

    def analyze(
        self,
        document: Document,
        context_documents: list[Document] | None = None,
        retrieval_query: str | None = None,
    ) -> AnalysisResult:
        entities: list[Entity] = []
        for extractor in self.extractors:
            entities.extend(extractor.extract(document))

        hits: list[RetrievalHit] = []
        if retrieval_query and context_documents:
            for agent in self.retrieval_agents:
                hits.extend(agent.retrieve(retrieval_query, context_documents))
            hits.sort(key=lambda hit: hit.score, reverse=True)

        return AnalysisResult(
            document_id=document.doc_id,
            entities=entities,
            retrieval_hits=hits,
            diagnostics={
                "extractor_count": str(len(self.extractors)),
                "retrieval_agent_count": str(len(self.retrieval_agents)),
            },
        )
