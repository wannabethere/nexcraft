from __future__ import annotations

from typing import Protocol

from ontology_foundry.models import Document, RetrievalHit


class RetrievalAgent(Protocol):
    name: str

    def retrieve(self, query: str, context_documents: list[Document]) -> list[RetrievalHit]:
        ...


class KeywordRetrievalAgent:
    name = "keyword-retrieval"

    def retrieve(self, query: str, context_documents: list[Document]) -> list[RetrievalHit]:
        terms = {token.lower() for token in query.split() if token.strip()}
        hits: list[RetrievalHit] = []
        for document in context_documents:
            lowered = document.text.lower()
            overlap = sum(1 for term in terms if term in lowered)
            if overlap == 0:
                continue
            score = overlap / max(1, len(terms))
            hits.append(
                RetrievalHit(
                    chunk_id=document.doc_id,
                    content=document.text[:280],
                    score=score,
                    metadata=document.metadata,
                )
            )
        return sorted(hits, key=lambda hit: hit.score, reverse=True)


class LlmRetrievalAgent:
    name = "llm-retrieval"

    def __init__(self, provider: str = "openai", model: str = "gpt-4o-mini") -> None:
        self.provider = provider
        self.model = model

    def retrieve(self, query: str, context_documents: list[Document]) -> list[RetrievalHit]:
        fallback = context_documents[0] if context_documents else Document(doc_id="none", text="")
        return [
            RetrievalHit(
                chunk_id=fallback.doc_id,
                content=fallback.text[:280],
                score=0.0,
                metadata={"provider": self.provider, "model": self.model, "query": query},
            )
        ]
