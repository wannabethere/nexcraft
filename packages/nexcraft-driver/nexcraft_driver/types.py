"""Wire types for the FlightSQL driver's async-query surface.

The async path is intentionally separate from synchronous `CommandStatementQuery`
execution: callers submit a query, get a handle back immediately, then poll
for status and fetch when ready. Same protocol any backend will implement
(in-process today, Temporal-backed later).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class QueryState(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    SUCCEEDED = "succeeded"
    FAILED    = "failed"
    CANCELLED = "cancelled"
    EXPIRED   = "expired"


@dataclass
class SubmitRequest:
    """Request body for SubmitQuery — the same shape used for sync execution
    minus the streaming part."""
    source_id: str
    sql: str
    tenant_id: str = "default"
    request_id: Optional[str] = None
    deadline_seconds: Optional[int] = None
    cache_mode: str = "default"   # forwarded; the driver doesn't act on it in v0.0.1
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")

    @classmethod
    def from_bytes(cls, payload: bytes) -> "SubmitRequest":
        return cls(**json.loads(payload.decode("utf-8")))


@dataclass
class QueryHandle:
    """Returned by SubmitQuery. Clients echo this in poll / fetch / cancel."""
    query_id: str
    submitted_at: str   # ISO-8601 UTC

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")

    @classmethod
    def from_bytes(cls, payload: bytes) -> "QueryHandle":
        return cls(**json.loads(payload.decode("utf-8")))


@dataclass
class QueryStatus:
    """Returned by GetQueryStatus."""
    query_id: str
    state: QueryState
    rows: Optional[int] = None
    bytes: Optional[int] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error_class: Optional[str] = None
    error_message: Optional[str] = None
    result_uri: Optional[str] = None

    def to_bytes(self) -> bytes:
        d = asdict(self)
        d["state"] = self.state.value
        return json.dumps(d).encode("utf-8")

    @classmethod
    def from_bytes(cls, payload: bytes) -> "QueryStatus":
        d = json.loads(payload.decode("utf-8"))
        d["state"] = QueryState(d["state"])
        return cls(**d)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()
