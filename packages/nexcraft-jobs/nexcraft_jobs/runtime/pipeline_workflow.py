"""Generic dstools multi-step pipeline workflow (W1.0b).

Runs an ordered list of steps — SQL extracts (FedSQL), dstools sql_template
tools, and dstools python tools that consume prior steps' rows — and returns
the chosen step's result as an ``ExecutionResult``. This is the target of the
multi-step nexcraft YAML the SQL agent generates for ``EXECUTION STRATEGY:
multi_step`` reasoning plans (e.g. SQL extract → Shapley/ARIMA → assemble).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from nexcraft_jobs.runtime.pipeline_models import (
    DstoolsPipelineInput,
    PipelineStep,
    tool_output_to_execution_result,
)
from nexcraft_jobs.schemas import ExecutionResult, FedSQLQueryInput

__all__ = ["DstoolsPipelineWorkflow", "tool_output_to_execution_result"]


@workflow.defn(name="nexcraft_dstools_pipeline")
class DstoolsPipelineWorkflow:
    @workflow.run
    async def run(self, params: DstoolsPipelineInput | dict[str, Any]) -> ExecutionResult:
        payload = (
            params
            if isinstance(params, DstoolsPipelineInput)
            else DstoolsPipelineInput.model_validate(params)
        )
        with workflow.unsafe.imports_passed_through():
            from dstools.contracts.inputs import PythonToolInput, SqlTemplateInput

            from nexcraft_jobs.runtime.dstools_tool_activities import (
                run_python_tool,
                run_sql_template,
            )
            from nexcraft_jobs.runtime.fedsql_activities import fedsql_execute_to_dataframe

        retry = RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=3)
        sql_timeout = timedelta(seconds=payload.timeout_seconds)
        py_timeout = timedelta(seconds=payload.timeout_seconds)
        results: dict[str, ExecutionResult] = {}

        async def _run_fedsql(sql: str, step: PipelineStep) -> ExecutionResult:
            fed = FedSQLQueryInput(
                sql=sql,
                source_id=step.source_id,
                dialect=step.dialect,
                tenant_id=payload.tenant_id,
                row_limit=payload.row_limit,
                timeout_seconds=payload.timeout_seconds,
            )
            raw = await workflow.execute_activity(
                fedsql_execute_to_dataframe,
                fed,
                start_to_close_timeout=sql_timeout,
                retry_policy=retry,
            )
            return raw if isinstance(raw, ExecutionResult) else ExecutionResult.model_validate(raw)

        for step in payload.steps:
            if step.kind == "fedsql":
                results[step.id] = await _run_fedsql(step.sql or "", step)

            elif step.kind == "dstools_sql":
                rendered = await workflow.execute_activity(
                    run_sql_template,
                    SqlTemplateInput(template=step.tool, params=step.params, dialect=step.dialect),
                    start_to_close_timeout=sql_timeout,
                    retry_policy=retry,
                )
                results[step.id] = await _run_fedsql(str(rendered), step)

            elif step.kind == "dstools_python":
                params_in = dict(step.params)
                if step.consumes and step.consumes in results:
                    # Feed the prior step's rows into the python tool.
                    params_in[step.data_param] = results[step.consumes].rows
                out = await workflow.execute_activity(
                    run_python_tool,
                    PythonToolInput(tool=step.tool, params=params_in),
                    start_to_close_timeout=py_timeout,
                    retry_policy=retry,
                )
                results[step.id] = tool_output_to_execution_result(out)

        final_id = payload.resolved_final()
        final = results.get(
            final_id,
            ExecutionResult(success=False, errors=[f"no result for final step {final_id!r}"]),
        )
        if payload.post_output and final.success:
            with workflow.unsafe.imports_passed_through():
                from nexcraft_jobs.runtime.genieml_post_output import enrich_with_post_output

            return await enrich_with_post_output(
                workflow,
                data=final,
                post=payload.post_output,
                retry=retry,
                timeout=py_timeout,
            )
        return final
