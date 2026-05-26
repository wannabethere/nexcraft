from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from nexcraft_jobs.runtime.temporal_payloads import SubmitJobPayload
from nexcraft_jobs.types import ResultRef


@workflow.defn(name="nexcraft_recipe_inline")
class RecipeInlineWorkflow:
    """Minimal durable wrapper; upgrade to four-activity staging per jobs/02-temporal.md."""

    @workflow.run
    async def run(self, payload: SubmitJobPayload) -> ResultRef:
        await workflow.execute_activity(
            "validate_recipe_activity",
            payload,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return await workflow.execute_activity(
            "run_recipe_inline_activity",
            payload,
            start_to_close_timeout=timedelta(hours=2),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=5),
                maximum_attempts=3,
            ),
        )
