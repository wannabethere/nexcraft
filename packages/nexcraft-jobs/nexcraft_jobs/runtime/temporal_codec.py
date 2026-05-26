"""Decode Temporal payloads into runtime objects (activities/workers only)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nexcraft.core.context import QueryContext

from nexcraft_jobs.context import JobContext
from nexcraft_jobs.runtime.temporal_payloads import SubmitJobPayload


def job_context_from_submit_payload(payload: SubmitJobPayload) -> JobContext:
    """Rehydrate a JobContext inside an activity from the workflow's payload.

    The cancel ``asyncio.Event`` is recreated fresh per activity (Temporal
    cancellation flows through ``activity.is_cancelled``), and the QueryContext
    is left out — recipes derive it from the JobContext via
    ``ctx.derive_query_context``.
    """
    submitted_at = payload.submitted_at or datetime.now(timezone.utc)
    qc = QueryContext(
        tenant_id=payload.tenant_id,
        query_id=payload.query_id,
        trace_id=payload.trace_id,
    )
    return JobContext(
        tenant_id=payload.tenant_id,
        job_id=payload.job_id,
        recipe_name=payload.recipe_name,
        recipe_version=payload.recipe_version,
        submitted_at=submitted_at,
        workflow_id=payload.workflow_id,
        trace_id=payload.trace_id,
        extract_row_budget=payload.extract_row_budget,
        extract_byte_budget=payload.extract_byte_budget,
        extract_deadline_per_query=timedelta(seconds=payload.extract_deadline_seconds),
        memory_budget=payload.memory_budget,
        cpu_budget=payload.cpu_budget,
        scratch_dir=payload.scratch_dir,
        job_deadline=payload.job_deadline,
        query=qc,
    )
