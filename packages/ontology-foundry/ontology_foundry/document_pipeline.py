from __future__ import annotations

from dataclasses import dataclass

from ontology_foundry.chunkers import DocumentChunker, RecursiveTextChunker
from ontology_foundry.models import AnalysisResult, Document, Entity, EntitySpanArtifact
from ontology_foundry.ner.pipeline import HybridNerConfig, HybridNerPipeline


@dataclass
class FoundryDocumentPipeline:
    """
    Document extractors path from foundry §4.1: chunk → hybrid NER → span artifacts.
    Intended for ingestion Stage 2 alongside schema/tabular extractors.
    """

    chunker: DocumentChunker
    ner: HybridNerPipeline

    @classmethod
    def default(cls) -> FoundryDocumentPipeline:
        return cls(chunker=RecursiveTextChunker(), ner=HybridNerPipeline(HybridNerConfig()))

    def run(self, document: Document) -> tuple[list[EntitySpanArtifact], list[Entity]]:
        chunks = self.chunker.chunk(document)
        artifacts: list[EntitySpanArtifact] = []
        legacy: list[Entity] = []
        for ch in chunks:
            art = self.ner.extract_chunk(ch)
            artifacts.append(art)
            for span in art.spans:
                legacy.append(
                    Entity(
                        label=span.span_type,
                        text=span.text,
                        start=span.char_start,
                        end=span.char_end,
                        confidence=span.confidence,
                        source=span.source_model,
                    )
                )
        return artifacts, legacy

    def analyze(self, document: Document) -> AnalysisResult:
        artifacts, legacy_entities = self.run(document)
        return AnalysisResult(
            document_id=document.doc_id,
            entities=legacy_entities,
            span_artifacts=artifacts,
            diagnostics={
                "chunk_count": str(len(artifacts)),
                "span_count": str(sum(len(a.spans) for a in artifacts)),
            },
        )
