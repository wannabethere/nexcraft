"""Run GenieML summary + chart activities after SQL/data steps complete."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio.common import RetryPolicy

from nexcraft_jobs.schemas import ExecutionResult, GeniemlPostOutputConfig


def execution_preview(result: ExecutionResult) -> dict[str, Any]:
    rows = result.rows[:50] if result.rows else []
    col_vals: dict[str, list[Any]] = {}
    for row in rows:
        if isinstance(row, dict):
            for k, v in row.items():
                col_vals.setdefault(k, []).append(v)
    return {
        "rows": rows,
        "row_count": result.row_count,
        "columns": [c.name for c in result.columns],
        "sample_column_values": col_vals,
    }


async def enrich_with_post_output(
    workflow_mod: Any,
    *,
    data: ExecutionResult,
    post: GeniemlPostOutputConfig,
    retry: RetryPolicy,
    timeout: timedelta,
) -> ExecutionResult:
    """Execute optional narrate + chart activities; attach artifacts under metadata."""
    if not post.summarize and not post.chart:
        return data

    with workflow_mod.unsafe.imports_passed_through():
        from nexcraft_jobs.runtime.genieml_output_activities import (
            genieml_chart_vega,
            genieml_narrate_result,
        )
        from nexcraft_jobs.runtime.genieml_output_models import (
            GeniemlChartParams,
            GeniemlNarrateParams,
        )

    preview = execution_preview(data)
    metadata: dict[str, Any] = dict(data.metadata or {})

    if post.summarize:
        summary = await workflow_mod.execute_activity(
            genieml_narrate_result,
            GeniemlNarrateParams(
                question=post.question,
                sql=post.sql,
                conversation_id=post.conversation_id,
                org_id=post.org_id,
                row_count=data.row_count,
                result_preview=preview,
            ),
            start_to_close_timeout=timeout,
            retry_policy=retry,
        )
        if isinstance(summary, dict):
            metadata["summary"] = summary

    if post.chart:
        chart = await workflow_mod.execute_activity(
            genieml_chart_vega,
            GeniemlChartParams(
                question=post.question,
                sql=post.sql,
                conversation_id=post.conversation_id,
                org_id=post.org_id,
                language=post.language,
                sample_data=preview.get("rows") or [],
                sample_column_values=preview.get("sample_column_values") or {},
            ),
            start_to_close_timeout=timeout,
            retry_policy=retry,
        )
        if isinstance(chart, dict):
            metadata["chart"] = chart

    return data.model_copy(update={"metadata": metadata})
