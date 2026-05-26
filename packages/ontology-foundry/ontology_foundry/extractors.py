from __future__ import annotations

from typing import Protocol

from ontology_foundry.models import Document, Entity
from ontology_foundry.ner.stages import CausalMarkerStage


class EntityExtractor(Protocol):
    name: str

    def extract(self, document: Document) -> list[Entity]:
        ...


class RuleBasedNerExtractor:
    """Causal-marker lexicon only (§3.6 rule-based scanner)."""

    name = "rule-based-ner"

    def __init__(self) -> None:
        self._stage = CausalMarkerStage()

    def extract(self, document: Document) -> list[Entity]:
        entities: list[Entity] = []
        for span in self._stage.extract(document.text):
            entities.append(
                Entity(
                    label=span.span_type,
                    text=span.text,
                    start=span.char_start,
                    end=span.char_end,
                    confidence=span.confidence,
                    source=span.source_model,
                )
            )
        return entities


class HuggingFaceNerExtractor:
    """Token-classification NER via transformers when installed."""

    name = "huggingface-ner"

    def __init__(self, model_name: str = "dslim/bert-base-NER") -> None:
        self.model_name = model_name
        self._nlp: object | None = None

    def _ensure(self) -> bool:
        if self._nlp is not None:
            return True
        try:
            from transformers import pipeline

            self._nlp = pipeline(
                "ner",
                model=self.model_name,
                tokenizer=self.model_name,
                aggregation_strategy="simple",
            )
            return True
        except Exception:
            self._nlp = None
            return False

    def extract(self, document: Document) -> list[Entity]:
        if not document.text.strip():
            return []
        if not self._ensure() or self._nlp is None:
            stage = CausalMarkerStage()
            return [
                Entity(
                    label=s.span_type,
                    text=s.text,
                    start=s.char_start,
                    end=s.char_end,
                    confidence=s.confidence,
                    source=f"{self.name}:{self.model_name}|fallback-rules",
                )
                for s in stage.extract(document.text)
            ]
        nlp = self._nlp
        ents = nlp(document.text)
        out: list[Entity] = []
        if isinstance(ents, dict):
            ents = [ents]
        for e in ents:
            if not isinstance(e, dict):
                continue
            label = str(e.get("entity_group") or e.get("label") or "ENT")
            score = float(e.get("score", 0.0))
            start = int(e.get("start", 0))
            end = int(e.get("end", 0))
            word = str(e.get("word", document.text[start:end]))
            out.append(
                Entity(
                    label=label,
                    text=word,
                    start=start,
                    end=end,
                    confidence=score,
                    source=f"{self.name}:{self.model_name}",
                )
            )
        return out
