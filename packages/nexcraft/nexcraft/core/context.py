from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class QueryContext:
    tenant_id: str
    query_id: str
    trace_id: Optional[str] = None
    parent_span_id: Optional[str] = None
    deadline: Optional[datetime] = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    max_rows: Optional[int] = None
    max_bytes: Optional[int] = None
    target_partitions: int = 4
    batch_size_hint: int = 8192
    tags: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id must be non-empty")
        if not self.query_id:
            raise ValueError("query_id must be non-empty")
