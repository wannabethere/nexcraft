"""Generate a SeedPack YAML from a Microsoft CDM (Common Data Model) folder.

Offline tooling. Point it at a checkout of https://github.com/microsoft/CDM,
specifically one of the `schemaDocuments/.../crmCommon/<domain>/` subfolders.
The output is committed as a regular pack alongside the hand-authored ones.

Usage:
    python -m ontology_foundry.relations.seeds.cdm \\
        --in /path/to/CDM/schemaDocuments/.../crmCommon/sales \\
        --out ontology_foundry/relations/seeds/packs/sales.yaml \\
        --name sales

CDM caveats (see relations/seeds/README in the design notes):
  * `extendsEntity` inheritance is NOT followed — only attributes declared
    directly on each entity are walked. Sufficient for predicate discovery;
    swap in CDM's resolver if you need full coverage.
  * `entity.entityReference` is the relationship marker. Other CDM constructs
    (constant entities, trait references) are ignored on purpose.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import yaml


def import_cdm_folder(folder: Path, pack_name: str) -> dict[str, Any]:
    """Walk `*.cdm.json` files under `folder` and emit a serializable pack dict."""
    seeds: list[dict[str, Any]] = []
    for cdm_file in sorted(folder.rglob("*.cdm.json")):
        try:
            doc = json.loads(cdm_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for definition in doc.get("definitions", []) or []:
            entity_name = definition.get("entityName")
            if not isinstance(entity_name, str):
                continue
            for attr in definition.get("hasAttributes", []) or []:
                ref = _attribute_entity_reference(attr)
                if not ref or ref == entity_name:
                    continue
                attr_name = attr.get("name") or attr.get("displayName") or ""
                predicate = _predicate_from_attribute(str(attr_name), ref)
                if not predicate:
                    continue
                description = (
                    str(attr.get("description"))
                    if attr.get("description")
                    else f"{entity_name}.{attr_name} → {ref} (from CDM)."
                )
                seeds.append(
                    {
                        "predicate": predicate,
                        "description": description,
                        "preferred_domain": [entity_name],
                        "preferred_range": [ref],
                    }
                )

    return {
        "name": pack_name,
        "description": f"Generated from Microsoft CDM at {folder}.",
        "source": "cdm",
        "version": 1,
        "seeds": _dedupe_seeds(seeds),
    }


def _attribute_entity_reference(attr: Any) -> str | None:
    """Return the referenced entity name when `attr` is an entity-typed attribute."""
    if not isinstance(attr, dict):
        return None
    ent = attr.get("entity")
    if isinstance(ent, str):
        return ent
    if isinstance(ent, dict):
        ref = ent.get("entityReference")
        if isinstance(ref, str):
            return ref
        if isinstance(ref, dict):
            name = ref.get("entityName") or ref.get("source")
            if isinstance(name, str):
                return name
    return None


def _predicate_from_attribute(attr_name: str, ref_entity: str) -> str:
    """`primarycontactid` → `has_primary_contact`, `parentAccountId` → `has_parent_account`."""
    base = re.sub(r"id$", "", attr_name, flags=re.IGNORECASE).strip("_ ")
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", base).lower()
    snake = re.sub(r"[^a-z0-9]+", "_", snake).strip("_")
    if not snake:
        snake = re.sub(r"[^a-z0-9]+", "_", ref_entity.lower()).strip("_")
    if not snake:
        return ""
    return snake if snake.startswith("has_") else f"has_{snake}"


def _dedupe_seeds(seeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse identical (predicate, domain, range) triples; keep first occurrence."""
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for seed in seeds:
        domain = seed["preferred_domain"][0] if seed["preferred_domain"] else ""
        rng = seed["preferred_range"][0] if seed["preferred_range"] else ""
        key = (seed["predicate"], domain, rng)
        if key in seen:
            continue
        seen.add(key)
        out.append(seed)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a SeedPack YAML from a Microsoft CDM folder.",
    )
    parser.add_argument(
        "--in",
        dest="src",
        required=True,
        type=Path,
        help="Path to a CDM schemaDocuments subfolder (e.g. .../crmCommon/sales).",
    )
    parser.add_argument(
        "--out",
        dest="dst",
        required=True,
        type=Path,
        help="Output YAML path. Commit this file alongside other packs.",
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Pack name (e.g. 'sales', 'service').",
    )
    args = parser.parse_args()

    pack = import_cdm_folder(args.src, args.name)
    args.dst.parent.mkdir(parents=True, exist_ok=True)
    args.dst.write_text(yaml.safe_dump(pack, sort_keys=False), encoding="utf-8")
    print(f"Wrote {len(pack['seeds'])} seeds → {args.dst}")


if __name__ == "__main__":
    main()
