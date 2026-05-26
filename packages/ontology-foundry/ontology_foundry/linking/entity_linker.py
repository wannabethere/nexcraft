from __future__ import annotations

from dataclasses import dataclass, field

from ontology_foundry.models import EntitySpan


@dataclass
class SeedFirstEntityLinker:
    """
    Foundry §4.1 entity linker: exact match against seed concepts first,
    embedding similarity is pluggable later (Qdrant).
    """

    concepts_by_normalized_surface: dict[str, str] = field(default_factory=dict)

    def link(self, span: EntitySpan) -> EntitySpan:
        key = normalize_for_exact_lookup(span.text)
        anchor = self.concepts_by_normalized_surface.get(key)
        if anchor is None:
            return span
        return span.model_copy(update={"seed_anchor": anchor})


def normalize_for_exact_lookup(text: str) -> str:
    return " ".join(text.strip().lower().split())
