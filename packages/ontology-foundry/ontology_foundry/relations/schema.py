"""Induced relation schema — the TBox produced by `induction.induce_schema()`.

Distinct from `seeds/`: seeds are *input vocabulary hints*; a `RelationSchema`
is the *output* of running the open extractor over a corpus and aggregating
observed (subject_type, object_type) pairs per canonical predicate.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RelationType:
    """One row in the induced TBox: predicate + observed dominant domain/range."""

    predicate: str
    domain: str
    range: str
    inverse: str | None = None
    functional: bool = False


@dataclass(frozen=True)
class RelationSchema:
    types: tuple[RelationType, ...] = field(default_factory=tuple)

    def predicates(self) -> tuple[str, ...]:
        return tuple(t.predicate for t in self.types)

    def by_predicate(self, predicate: str) -> RelationType | None:
        for t in self.types:
            if t.predicate == predicate:
                return t
        return None

    def allows(self, predicate: str, subject_type: str, object_type: str) -> bool:
        """For the optional hybrid mode: once you've induced a schema, freeze it
        and re-extract with `allows()` as a hard filter. Default extraction is
        seed-biased, not schema-constrained — this is opt-in."""
        t = self.by_predicate(predicate)
        return t is not None and t.domain == subject_type and t.range == object_type


__all__ = ["RelationSchema", "RelationType"]
