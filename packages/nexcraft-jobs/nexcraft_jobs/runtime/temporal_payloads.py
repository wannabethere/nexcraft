from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class SubmitJobPayload:
    """Workflow/activity payload: primitives only (Temporal workflow sandbox friendly)."""

    recipe_name: str
    recipe_version: str
    params: dict[str, Any]
    tenant_id: str
    job_id: str
    query_id: str
    workflow_id: str = ""
    trace_id: str | None = None

    submitted_at: datetime | None = None

    extract_row_budget: int | None = 50_000_000
    extract_byte_budget: int | None = None
    extract_deadline_seconds: float = 600.0

    memory_budget: str = "8GB"
    cpu_budget: int = 4
    scratch_dir: str | None = None

    job_deadline: datetime | None = None

    staging_root: str | None = None
