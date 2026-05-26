"""Catalog backed by a ManagementStore (host-system connections table)."""

from __future__ import annotations

from nexcraft.connection.management import ConnectionDetails, ManagementStore
from nexcraft.core.descriptors import SourceDescriptor


def _to_descriptor(row: ConnectionDetails) -> SourceDescriptor:
    return SourceDescriptor(
        source_id=row.source_id,
        kind=row.kind,
        display_name=row.display_name,
        tenant_id=row.tenant_id,
        config=dict(row.config),
        tags=dict(row.tags),
    )


class DBCatalog:
    """Pulls SourceDescriptors from a ManagementStore on each request.

    No internal caching: the catalog always reflects the current state of the
    backing store. If you need caching, wrap this in a TTL layer at the call
    site rather than baking it in here — the right TTL depends on how the
    host system rotates secrets and renames sources.
    """

    def __init__(self, store: ManagementStore) -> None:
        self._store = store

    async def get_source(self, source_id: str) -> SourceDescriptor:
        row = await self._store.get_connection_details(source_id)
        return _to_descriptor(row)

    async def list_sources(self, tenant_id: str | None = None) -> list[SourceDescriptor]:
        rows = await self._store.list_connection_details(tenant_id=tenant_id)
        return [_to_descriptor(r) for r in rows]


__all__ = ["DBCatalog"]
