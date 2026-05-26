"""Temporal activities — production uses staged Parquet handles per jobs/02-temporal.md."""

from __future__ import annotations

from datetime import timedelta

from temporalio import activity
from temporalio.common import RetryPolicy

from nexcraft_jobs.runtime.local import LocalRuntime
from nexcraft_jobs.runtime.registry import GLOBAL_REGISTRY
from nexcraft_jobs.runtime.temporal_codec import job_context_from_submit_payload
from nexcraft_jobs.runtime.temporal_payloads import SubmitJobPayload
from nexcraft_jobs.runtime.worker_config import get_worker_fedsql, get_worker_store
from nexcraft_jobs.types import ResultRef


@activity.defn
async def validate_recipe_activity(payload: SubmitJobPayload) -> None:
    recipe = GLOBAL_REGISTRY.get(payload.recipe_name, payload.recipe_version)
    recipe.validate(payload.params)


@activity.defn
async def run_recipe_inline_activity(payload: SubmitJobPayload) -> ResultRef:
    """Single-activity pipeline for development; replace with staged activities at scale."""
    recipe = GLOBAL_REGISTRY.get(payload.recipe_name, payload.recipe_version)
    ctx = job_context_from_submit_payload(payload)
    runtime = LocalRuntime(get_worker_fedsql(), get_worker_store())
    return await runtime.submit(recipe, payload.params, ctx)


DEFAULT_INLINE_ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    maximum_attempts=3,
)

DEFAULT_INLINE_ACTIVITY_TIMEOUT = timedelta(hours=2)
