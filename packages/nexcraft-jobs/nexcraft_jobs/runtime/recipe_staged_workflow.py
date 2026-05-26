"""Multi-activity recipe workflow with Parquet staging.

Orchestration patterns (RetryPolicy, heartbeat_timeout, sandbox imports) mirror
``leen_connectors/workflows/connector_workflow.py`` in leen-security.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from nexcraft_jobs.runtime.temporal_payloads import SubmitJobPayload
    from nexcraft_jobs.types import ResultRef

_EXTRACT_NON_RETRYABLE = [
    "BudgetExceededError",
    "SourceSyntaxError",
    "AuthenticationError",
    "ConfigurationError",
    "CancelledError",
    "SchemaMismatchError",
    "ValueError",
    "KeyError",
]


@workflow.defn(name="nexcraft_recipe_staged")
class RecipeStagedWorkflow:
    """validate → extract (Parquet staging + heartbeats) → compute → persist."""

    @workflow.run
    async def run(self, payload: SubmitJobPayload) -> ResultRef:
        await workflow.execute_activity(
            "validate_recipe_activity",
            args=[payload],
            schedule_to_start_timeout=timedelta(minutes=5),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

        extract_results = await workflow.execute_activity(
            "run_extract_to_parquet_activity",
            args=[payload],
            schedule_to_start_timeout=timedelta(minutes=10),
            start_to_close_timeout=timedelta(minutes=20),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=5),
                maximum_attempts=3,
                non_retryable_error_types=_EXTRACT_NON_RETRYABLE,
            ),
        )

        compute_result = await workflow.execute_activity(
            "run_compute_from_parquet_activity",
            args=[payload, extract_results],
            schedule_to_start_timeout=timedelta(minutes=10),
            start_to_close_timeout=timedelta(minutes=60),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=5),
                maximum_attempts=2,
                non_retryable_error_types=[
                    *_EXTRACT_NON_RETRYABLE,
                    "InternalError",
                ],
            ),
        )

        return await workflow.execute_activity(
            "run_persist_activity",
            args=[payload, compute_result],
            schedule_to_start_timeout=timedelta(minutes=10),
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=5),
                maximum_attempts=5,
                non_retryable_error_types=[
                    "ConfigurationError",
                    "ValueError",
                    "KeyError",
                ],
            ),
        )
