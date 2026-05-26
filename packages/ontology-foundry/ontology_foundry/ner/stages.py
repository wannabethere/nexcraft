from __future__ import annotations

import re
from dataclasses import dataclass, field

from ontology_foundry.models import EntitySpan
from ontology_foundry.ner.lexicon import DEFAULT_CAUSAL_MARKERS


@dataclass
class SpacyNerStage:
    """spaCy transformer/large pipeline when installed (§4.3)."""

    model_name: str = "en_core_web_sm"
    _nlp: object | None = field(default=None, repr=False)

    def ensure(self) -> bool:
        if self._nlp is not None:
            return True
        try:
            import spacy

            self._nlp = spacy.load(self.model_name)
            return True
        except Exception:
            self._nlp = None
            return False

    def extract(self, text: str) -> list[EntitySpan]:
        if not self.ensure() or not text:
            return []
        nlp = self._nlp
        assert nlp is not None
        doc = nlp(text)
        out: list[EntitySpan] = []
        for ent in doc.ents:
            out.append(
                EntitySpan(
                    text=ent.text,
                    span_type=ent.label_,
                    source_model=f"spacy:{self.model_name}",
                    char_start=int(ent.start_char),
                    char_end=int(ent.end_char),
                    confidence=0.9,
                )
            )
        return out


@dataclass
class CapitalizedFallbackStage:
    """Cheap stand-in when spaCy is unavailable (dev/tests only)."""

    name: str = "capitalized-fallback"

    def extract(self, text: str) -> list[EntitySpan]:
        out: list[EntitySpan] = []
        for m in re.finditer(r"\b(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", text):
            out.append(
                EntitySpan(
                    text=m.group(0),
                    span_type="PROPER_NOUN",
                    source_model=self.name,
                    char_start=m.start(),
                    char_end=m.end(),
                    confidence=0.45,
                )
            )
        return out


@dataclass
class GlinerNerStage:
    """
    GLiNER zero-shot NER (§3.6). Uses `gliner` when installed; otherwise falls back
    to keyword-style detection for seed `ner_labels`.
    """

    skip_model_load: bool = False
    model_name: str = "urchade/gliner_medium-v2.1"
    ner_labels: tuple[str, ...] = (
        "entity_name",
        "attribute",
        "concept",
        "event",
        "actor_role",
        "policy_reference",
        "quantitative_claim",
        "temporal_qualifier",
    )
    _model: object | None = field(default=None, repr=False)

    def _ensure_model(self) -> bool:
        if self.skip_model_load:
            return False
        if self._model is not None:
            return True
        try:
            from gliner import GLiNER

            self._model = GLiNER.from_pretrained(self.model_name)
            return True
        except Exception:
            self._model = None
            return False

    def extract(self, text: str) -> list[EntitySpan]:
        if not text:
            return []
        if self._ensure_model():
            return self._predict_gliner(text)
        return self._fallback_seed_labels(text)

    def _predict_gliner(self, text: str) -> list[EntitySpan]:
        model = self._model
        assert model is not None
        labels = list(self.ner_labels)
        try:
            ents = model.predict_entities(text, labels, threshold=0.28)
        except Exception:
            return self._fallback_seed_labels(text)
        out: list[EntitySpan] = []
        for e in ents:
            if not isinstance(e, dict):
                continue
            start = int(e.get("start", e.get("start_index", 0)))
            end = int(e.get("end", e.get("end_index", 0)))
            label = str(e.get("label", "concept"))
            score = float(e.get("score", 0.5))
            surface = text[start:end] if 0 <= start < end <= len(text) else str(e.get("text", ""))
            out.append(
                EntitySpan(
                    text=surface,
                    span_type=label,
                    source_model=f"gliner:{self.model_name}",
                    char_start=start,
                    char_end=end,
                    confidence=score,
                )
            )
        return out

    def _fallback_seed_labels(self, text: str) -> list[EntitySpan]:
        """Deterministic placeholder when GLiNER is unavailable: quantitative_claim patterns."""
        out: list[EntitySpan] = []
        if "quantitative_claim" not in self.ner_labels:
            return out
        for m in re.finditer(
            r"\b(?:by\s*)?(?:~?\d+(?:\.\d+)?%|\d+(?:\.\d+)?\s*(?:percent|bps))\b",
            text,
            flags=re.IGNORECASE,
        ):
            out.append(
                EntitySpan(
                    text=m.group(0),
                    span_type="quantitative_claim",
                    source_model="gliner-fallback",
                    char_start=m.start(),
                    char_end=m.end(),
                    confidence=0.75,
                )
            )
        return out


@dataclass
class CausalMarkerStage:
    """Rule-based scan for causal lexicon phrases (longest match wins per start)."""

    markers: tuple[str, ...] = DEFAULT_CAUSAL_MARKERS

    def extract(self, text: str) -> list[EntitySpan]:
        if not text:
            return []
        lowered = text.lower()
        markers = sorted(self.markers, key=len, reverse=True)
        found: list[EntitySpan] = []
        used: list[tuple[int, int]] = []

        def overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
            return not (a[1] <= b[0] or a[0] >= b[1])

        i = 0
        n = len(text)
        while i < n:
            matched = False
            for phrase in markers:
                pl = len(phrase)
                if i + pl > n:
                    continue
                slice_ = lowered[i : i + pl]
                if slice_ != phrase:
                    continue
                before = lowered[i - 1] if i > 0 else " "
                after = lowered[i + pl] if i + pl < n else " "
                if phrase[0].isalnum() and before.isalnum():
                    continue
                if phrase[-1].isalnum() and after.isalnum():
                    continue
                span_coords = (i, i + pl)
                if any(overlaps(span_coords, u) for u in used):
                    break
                surface = text[i : i + pl]
                found.append(
                    EntitySpan(
                        text=surface,
                        span_type="causal_marker",
                        source_model="rule_based",
                        char_start=i,
                        char_end=i + pl,
                        confidence=1.0,
                    )
                )
                used.append(span_coords)
                i += pl
                matched = True
                break
            if not matched:
                i += 1
        return found
