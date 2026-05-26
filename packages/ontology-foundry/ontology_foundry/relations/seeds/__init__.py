"""Relation seed packs: domain-scoped predicate vocabularies that bias the LLM
relation extractor without enforcing a hard schema.

A `RelationSeed` is a hint, not a constraint. Domain/range fields are
*preferred* types — the LLM may still propose edges that don't match, and the
canonicalization pass uses observed types to induce the actual schema.

Packs live as YAML/JSON files in:
  - `ontology_foundry/relations/seeds/packs/` (shipped defaults)
  - `<cwd>/.ontology-foundry/seeds/` (project overrides)
  - `~/.ontology-foundry/seeds/` (user overrides)

Later directories win on duplicate pack names.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RelationSeed:
    """A predicate hint. Not enforced — the LLM may still propose other predicates."""

    predicate: str
    description: str = ""
    examples: tuple[str, ...] = ()
    preferred_domain: tuple[str, ...] = ()
    preferred_range: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeedPack:
    name: str
    seeds: tuple[RelationSeed, ...] = ()
    description: str = ""
    source: str = ""

    def predicates(self) -> tuple[str, ...]:
        return tuple(s.predicate for s in self.seeds)


def default_pack_dirs() -> list[Path]:
    """Resolution order: shipped → project → user. Later wins."""
    return [
        Path(__file__).parent / "packs",
        Path.cwd() / ".ontology-foundry" / "seeds",
        Path.home() / ".ontology-foundry" / "seeds",
    ]


__all__ = ["RelationSeed", "SeedPack", "default_pack_dirs"]
