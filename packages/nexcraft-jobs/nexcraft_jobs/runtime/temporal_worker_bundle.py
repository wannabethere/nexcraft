"""Default activity/workflow lists for ``temporalio.worker.Worker`` registration."""

from __future__ import annotations

from nexcraft_jobs.runtime.activities_multihop import duckdb_combine_hops, fetch_sql_server_rows
from nexcraft_jobs.runtime.dstools_tool_activities import run_python_tool, run_sql_template
from nexcraft_jobs.runtime.recipe_staged_workflow import RecipeStagedWorkflow
from nexcraft_jobs.runtime.temporal_activities import (
    run_recipe_inline_activity,
    validate_recipe_activity,
)
from nexcraft_jobs.runtime.temporal_staged_activities import (
    run_compute_from_parquet_activity,
    run_extract_to_parquet_activity,
    run_persist_activity,
)
from nexcraft_jobs.runtime.fedsql_activities import fedsql_execute_to_dataframe
from nexcraft_jobs.runtime.fedsql_workflow import FedSQLQueryWorkflow
from nexcraft_jobs.runtime.genieml_output_activities import genieml_chart_vega, genieml_narrate_result
from nexcraft_jobs.runtime.pipeline_workflow import DstoolsPipelineWorkflow
from nexcraft_jobs.runtime.temporal_workflows import RecipeInlineWorkflow
from nexcraft_jobs.runtime.workflows_multihop import CornerstoneMultiHopWorkflow

NEXCRAFT_DSTOOLS_TOOL_ACTIVITIES = [
    run_sql_template,
    run_python_tool,
]

NEXCRAFT_MULTIHOP_ACTIVITIES = [
    fetch_sql_server_rows,
    duckdb_combine_hops,
]

NEXCRAFT_MULTIHOP_WORKFLOWS = [
    CornerstoneMultiHopWorkflow,
]

NEXCRAFT_FEDSQL_ACTIVITIES = [
    fedsql_execute_to_dataframe,
    genieml_narrate_result,
    genieml_chart_vega,
]

NEXCRAFT_FEDSQL_WORKFLOWS = [
    FedSQLQueryWorkflow,
]

NEXCRAFT_PIPELINE_WORKFLOWS = [
    DstoolsPipelineWorkflow,
]

NEXCRAFT_RECIPE_ACTIVITIES = [
    validate_recipe_activity,
    run_recipe_inline_activity,
    run_extract_to_parquet_activity,
    run_compute_from_parquet_activity,
    run_persist_activity,
    *NEXCRAFT_DSTOOLS_TOOL_ACTIVITIES,
    *NEXCRAFT_MULTIHOP_ACTIVITIES,
    *NEXCRAFT_FEDSQL_ACTIVITIES,
]

NEXCRAFT_RECIPE_WORKFLOWS = [
    RecipeInlineWorkflow,
    RecipeStagedWorkflow,
    *NEXCRAFT_MULTIHOP_WORKFLOWS,
    *NEXCRAFT_FEDSQL_WORKFLOWS,
    *NEXCRAFT_PIPELINE_WORKFLOWS,
]

__all__ = [
    "CornerstoneMultiHopWorkflow",
    "DstoolsPipelineWorkflow",
    "FedSQLQueryWorkflow",
    "NEXCRAFT_PIPELINE_WORKFLOWS",
    "NEXCRAFT_FEDSQL_ACTIVITIES",
    "NEXCRAFT_FEDSQL_WORKFLOWS",
    "NEXCRAFT_DSTOOLS_TOOL_ACTIVITIES",
    "NEXCRAFT_MULTIHOP_ACTIVITIES",
    "NEXCRAFT_MULTIHOP_WORKFLOWS",
    "NEXCRAFT_RECIPE_ACTIVITIES",
    "NEXCRAFT_RECIPE_WORKFLOWS",
    "RecipeInlineWorkflow",
    "RecipeStagedWorkflow",
    "run_python_tool",
    "run_sql_template",
]
