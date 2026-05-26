"""Load seed packs from one or more directories.

Precedence: directories iterated in order; later directories override earlier
ones for packs with the same name. Within `compose(...)`, later names in the
selected list win on duplicate predicates.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import yaml

from ontology_foundry.relations.seeds import RelationSeed, SeedPack

_PACK_SUFFIXES = {".yaml", ".yml", ".json"}


def load_packs(directories: Iterable[Path]) -> dict[str, SeedPack]:
    """Walk each directory in order and return `{pack_name: SeedPack}`.

    Non-existent directories are silently skipped. Files with unsupported
    suffixes are ignored. Malformed pack files raise `PackLoadError` so a typo
    surfaces immediately rather than silently producing an empty pack.
    """
    registry: dict[str, SeedPack] = {}
    for directory in directories:
        if not directory.exists() or not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() not in _PACK_SUFFIXES:
                continue
            pack = _load_pack_file(path)
            registry[pack.name] = pack
    return registry


def compose(packs: dict[str, SeedPack], names: Iterable[str]) -> SeedPack:
    """Merge selected packs into a single stage-ready pack.

    Predicates from later packs override earlier ones with the same name.
    Unknown pack names are silently dropped — callers can `assert name in packs`
    first if they want to fail loudly.
    """
    merged: dict[str, RelationSeed] = {}
    chosen: list[str] = []
    for name in names:
        pack = packs.get(name)
        if pack is None:
            continue
        chosen.append(name)
        for seed in pack.seeds:
            merged[seed.predicate] = seed
    return SeedPack(
        name="+".join(chosen) if chosen else "empty",
        seeds=tuple(merged.values()),
        source="composed",
    )


class PackLoadError(ValueError):
    """Raised when a pack file is missing required fields or malformed."""


def _load_pack_file(path: Path) -> SeedPack:
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw) if path.suffix.lower() == ".json" else yaml.safe_load(raw)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise PackLoadError(f"failed to parse {path}: {exc}") from exc

    if not isinstance(data, dict) or "name" not in data:
        raise PackLoadError(f"{path}: missing required 'name' field")

    seeds_raw = data.get("seeds") or []
    if not isinstance(seeds_raw, list):
        raise PackLoadError(f"{path}: 'seeds' must be a list")

    seeds: list[RelationSeed] = []
    for idx, entry in enumerate(seeds_raw):
        if not isinstance(entry, dict) or "predicate" not in entry:
            raise PackLoadError(f"{path}: seed #{idx} missing 'predicate'")
        seeds.append(
            RelationSeed(
                predicate=str(entry["predicate"]),
                description=str(entry.get("description") or ""),
                examples=tuple(entry.get("examples") or ()),
                preferred_domain=tuple(entry.get("preferred_domain") or ()),
                preferred_range=tuple(entry.get("preferred_range") or ()),
            )
        )

    return SeedPack(
        name=str(data["name"]),
        seeds=tuple(seeds),
        description=str(data.get("description") or ""),
        source=str(data.get("source") or path.name),
    )


__all__ = ["PackLoadError", "compose", "load_packs"]
