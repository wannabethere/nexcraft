"""Generic single-SQL FedSQL workflow — Phase J.0."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from nexcraft_jobs.schemas import ExecutionResult, FedSQLQueryInput


@workflow.defn(name="nexcraft_fedsql_query")
class FedSQLQueryWorkflow:
    """Execute one SQL statement against a configured FedSQL source."""

    @workflow.run
    async def run(self, params: FedSQLQueryInput | dict[str, Any]) -> ExecutionResult:
        payload = (
            params
            if isinstance(params, FedSQLQueryInput)
            else FedSQLQueryInput.model_validate(params)
        )
        with workflow.unsafe.imports_passed_through():
            from nexcraft_jobs.runtime.fedsql_activities import fedsql_execute_to_dataframe

        raw = await workflow.execute_activity(
            fedsql_execute_to_dataframe,
            payload,
            start_to_close_timeout=timedelta(seconds=payload.timeout_seconds),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=2),
                maximum_attempts=3,
            ),
        )
        result = raw if isinstance(raw, ExecutionResult) else ExecutionResult.model_validate(raw)
        if payload.post_output:
            retry = RetryPolicy(
                initial_interval=timedelta(seconds=2),
                maximum_attempts=2,
            )
            with workflow.unsafe.imports_passed_through():
                from nexcraft_jobs.runtime.genieml_post_output import enrich_with_post_output

            return await enrich_with_post_output(
                workflow,
                data=result,
                post=payload.post_output,
                retry=retry,
                timeout=timedelta(seconds=min(payload.timeout_seconds, 300)),
            )
        return result
