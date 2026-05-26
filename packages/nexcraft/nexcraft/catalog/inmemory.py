from __future__ import annotations

from nexcraft.core.descriptors import SourceDescriptor
from nexcraft.errors import ConfigurationError


class InMemoryCatalog:
    def __init__(self, sources: dict[str, SourceDescriptor]) -> None:
        self._sources = dict(sources)

    async def get_source(self, source_id: str) -> SourceDescriptor:
        try:
            return self._sources[source_id]
        except KeyError as e:
            raise ConfigurationError(f"Unknown source_id={source_id!r}") from e

    async def list_sources(self, tenant_id: str | None = None) -> list[SourceDescriptor]:
        out = list(self._sources.values())
        if tenant_id is not None:
            out = [s for s in out if s.tenant_id == tenant_id]
        return out
