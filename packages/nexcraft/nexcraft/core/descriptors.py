from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ConnectionHandle:
    """Opaque handle returned by ConnectionProvider; executors narrow via subclasses."""

    source_id: str
    kind: str


@dataclass(frozen=True)
class SourceDescriptor:
    source_id: str
    kind: str
    display_name: str
    tenant_id: str
    config: dict[str, Any]
    tags: dict[str, str] = field(default_factory=dict)
