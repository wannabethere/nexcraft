"""YAML-backed Catalog reference implementation.

Loads source descriptors from a YAML file or string. Format::

    sources:
      - source_id: prod_pg
        kind: postgres
        display_name: "Production Postgres"
        tenant_id: default
        config:
          host: pg.internal
          database: prod
        tags:
          team: data

PyYAML is optional; if it isn't installed, ``YAMLCatalog.from_string`` raises
``ConfigurationError`` with an actionable message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nexcraft.catalog.inmemory import InMemoryCatalog
from nexcraft.core.descriptors import SourceDescriptor
from nexcraft.errors import ConfigurationError


def _require_yaml():
    try:
        import yaml  # type: ignore[import-not-found]

        return yaml
    except ModuleNotFoundError as exc:
        raise ConfigurationError(
            "YAMLCatalog requires PyYAML; install with `pip install pyyaml`."
        ) from exc


def _descriptor_from_dict(d: dict[str, Any]) -> SourceDescriptor:
    required = ("source_id", "kind", "display_name", "tenant_id")
    missing = [k for k in required if k not in d]
    if missing:
        raise ConfigurationError(
            f"YAML source entry missing required fields: {missing} (entry: {d!r})"
        )
    return SourceDescriptor(
        source_id=str(d["source_id"]),
        kind=str(d["kind"]),
        display_name=str(d["display_name"]),
        tenant_id=str(d["tenant_id"]),
        config=dict(d.get("config") or {}),
        tags=dict(d.get("tags") or {}),
    )


class YAMLCatalog(InMemoryCatalog):
    """In-memory catalog populated from a YAML document.

    Parses once at construction time; queries hit the in-memory dict. To pick
    up changes, reconstruct (e.g. on SIGHUP).
    """

    @classmethod
    def from_string(cls, text: str) -> "YAMLCatalog":
        yaml = _require_yaml()
        doc = yaml.safe_load(text) or {}
        if not isinstance(doc, dict):
            raise ConfigurationError("YAMLCatalog expects a mapping at the top level.")
        entries = doc.get("sources") or []
        if not isinstance(entries, list):
            raise ConfigurationError("YAMLCatalog expects 'sources' to be a list.")
        sources = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ConfigurationError(f"YAML source entry must be a mapping: {entry!r}")
            desc = _descriptor_from_dict(entry)
            if desc.source_id in sources:
                raise ConfigurationError(f"Duplicate source_id in YAML: {desc.source_id!r}")
            sources[desc.source_id] = desc
        return cls(sources)

    @classmethod
    def from_file(cls, path: str | Path) -> "YAMLCatalog":
        return cls.from_string(Path(path).read_text())
