"""Temporal harness for ontology-pipeline.

Exposes the pipeline as a Temporal workflow + activities so nexcraft-jobs'
YAML-based job submission can drive long-running ingestion runs end-to-end
with per-table retry, observability, and resumability.

Submission shape (compatible with `nexcraft_jobs.yaml_jobs.runner.run_job_spec`):

    version: 1
    name: csod-ontology-ingestion
    workflow_type: ontology_ingestion   # matches @workflow.defn(name=...)
    task_queue: ontology-pipeline-default
    input:
        source: { ... }   # matches `OntologyIngestionInput.source`
        pipeline: { ... } # matches `OntologyIngestionInput.pipeline`
        output: { ... }   # matches `OntologyIngestionInput.output`
        llm: { ... }

Per-table parallelism: the workflow fans out `process_one_table_activity`
via `asyncio.gather` so one slow / failing table doesn't serialize the rest.
Post-pass activities (relation induction, cross-asset causal, statistical
validation) run after the per-table fan-out completes.

Workflows + activities are importable without `temporalio` installed; only
the worker bootstrap and the runtime imports require it (extras: `[temporal]`).
"""
from ontology_pipeline.temporal.inputs import (
    OntologyIngestionInput,
    PerTableResult,
    PostPassResult,
    WorkflowSummary,
)

__all__ = [
    "OntologyIngestionInput",
    "PerTableResult",
    "PostPassResult",
    "WorkflowSummary",
]
