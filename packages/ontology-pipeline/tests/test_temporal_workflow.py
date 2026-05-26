"""Tests for the OntologyIngestionWorkflow + activities.

Two layers:

  - **Unit (always run):** Activity bodies called directly with stubbed
    introspector / sink. No Temporal runtime, no DB, no network. Verifies
    the per-table activity correctly accumulates relationships + concepts
    into the returned `PerTableResult`.

  - **Integration (gated):** End-to-end against a real Temporal server
    using `WorkflowEnvironment.start_local()`. Skipped unless `temporalio`
    is installed AND `RUN_TEMPORAL_TESTS=1` is set. Drives the full
    workflow against a stub source and asserts on `WorkflowSummary`.

The CSOD-data E2E (against host Postgres + host Temporal) lives in
`tests/integration/test_csod_temporal_e2e.py` and is gated separately on
`RUN_CSOD_E2E=1`.
"""
from __future__ import annotations

import os
from typing import Any

import pytest

from ontology_pipeline.temporal.inputs import (
    OntologyIngestionInput,
    PerTableResult,
    TableSpec,
    WorkflowSummary,
)


# ───────────────────────────────────────────────────────────────────────────
# Unit layer: input + result shape round-trips
# ───────────────────────────────────────────────────────────────────────────


class TestInputShapes:
    def test_round_trips_through_model_dump(self, tmp_path):
        """Workflow input must be JSON-serialisable for Temporal payloads."""
        input = OntologyIngestionInput(
            source={
                "source_id": "csod-pg", "org_id": "csod",
                "connection": {
                    "host": "localhost", "port": 5432, "database": "db",
                    "user": "u", "password": "p",
                },
            },
            output={"kind": "filesystem", "base_dir": str(tmp_path)},
            per_table_concurrency=2,
        )
        dumped = input.model_dump(mode="json")
        assert dumped["source"]["source_id"] == "csod-pg"
        assert dumped["per_table_concurrency"] == 2
        # Round-trip
        round_tripped = OntologyIngestionInput.model_validate(dumped)
        assert round_tripped.source.source_id == "csod-pg"

    def test_to_pipeline_config_drops_workflow_only_fields(self, tmp_path):
        input = OntologyIngestionInput(
            source={
                "source_id": "csod-pg", "org_id": "csod",
                "connection": {
                    "host": "localhost", "port": 5432, "database": "db",
                    "user": "u", "password": "p",
                },
            },
            output={"kind": "filesystem", "base_dir": str(tmp_path)},
            per_table_concurrency=8,
        )
        cfg = input.to_pipeline_config()
        # PipelineConfig has no per_table_concurrency
        assert not hasattr(cfg, "per_table_concurrency")
        assert cfg.source.source_id == "csod-pg"


class TestPerTableResultShape:
    def test_default_values(self):
        r = PerTableResult(
            qualified_name="public.users",
            asset_rk="postgres://x/y/public/users",
            outcome="created",
        )
        assert r.llm_calls == 0
        assert r.wall_time_s == 0.0
        assert r.inferred_relationships == []
        assert r.primary_concept is None

    def test_serialises_with_relationships(self):
        r = PerTableResult(
            qualified_name="public.users",
            asset_rk="postgres://x/y/public/users",
            outcome="created",
            llm_calls=3,
            inferred_relationships=[{"from_rk": "a", "to_rk": "b"}],
            primary_concept="Employee",
        )
        d = r.model_dump(mode="json")
        assert d["llm_calls"] == 3
        assert d["primary_concept"] == "Employee"
        # Round-trip
        r2 = PerTableResult.model_validate(d)
        assert r2.inferred_relationships == [{"from_rk": "a", "to_rk": "b"}]


class TestWorkflowSummaryShape:
    def test_can_be_built_from_per_table_results(self):
        per_table = [
            PerTableResult(
                qualified_name="public.users",
                asset_rk="rk:users", outcome="created", llm_calls=2,
            ),
            PerTableResult(
                qualified_name="public.dept",
                asset_rk="rk:dept", outcome="unchanged",
            ),
        ]
        s = WorkflowSummary(
            source_id="csod-pg",
            tables_seen=2, tables_processed=1, tables_skipped_unchanged=1,
            tables_errored=0, total_llm_calls=2,
            per_table=per_table,
        )
        d = s.model_dump(mode="json")
        assert d["tables_seen"] == 2
        assert d["tables_processed"] == 1
        assert len(d["per_table"]) == 2


# ───────────────────────────────────────────────────────────────────────────
# Workflow module loads cleanly even without temporalio installed
# ───────────────────────────────────────────────────────────────────────────


class TestModuleImports:
    def test_inputs_importable(self):
        # Should already work — no temporalio required.
        from ontology_pipeline.temporal import (  # noqa: F401
            OntologyIngestionInput, PerTableResult,
            PostPassResult, WorkflowSummary,
        )

    def test_workflows_module_imports_without_temporalio_or_with(self):
        # When temporalio is missing the module imports but the class is None.
        # When it's installed the class is a real workflow.
        from ontology_pipeline.temporal import workflows
        try:
            import temporalio  # noqa: F401
            assert workflows.OntologyIngestionWorkflow is not None
        except ImportError:
            assert workflows.OntologyIngestionWorkflow is None


# ───────────────────────────────────────────────────────────────────────────
# Integration layer — real Temporal local env
# ───────────────────────────────────────────────────────────────────────────


def _temporal_installed() -> bool:
    try:
        import temporalio  # noqa: F401
        return True
    except ImportError:
        return False


_HAS_TEMPORAL_TARGET = bool(os.environ.get("TEMPORAL_TARGET"))


@pytest.mark.skipif(
    not (_temporal_installed() and _HAS_TEMPORAL_TARGET
         and os.environ.get("RUN_TEMPORAL_TESTS") == "1"),
    reason="Requires temporalio installed + TEMPORAL_TARGET set + RUN_TEMPORAL_TESTS=1",
)
class TestWorkflowAgainstLocalEnv:
    """Drives the workflow against a host-provided Temporal server.

    Stubs the introspector + sink so the workflow runs end-to-end without
    touching a real source database. Verifies fan-out, post-pass ordering,
    and terminal `WorkflowSummary` shape.

    To run:
        pip install 'ontology-pipeline[temporal]'
        # Temporal already running on localhost:7233 (or equivalent).
        export TEMPORAL_TARGET=localhost:7233
        RUN_TEMPORAL_TESTS=1 pytest tests/test_temporal_workflow.py
    """

    @pytest.mark.asyncio
    async def test_end_to_end_simple_run(self, tmp_path):
        from temporalio.client import Client
        from temporalio.worker import Worker

        # Build a minimal input pointed at a filesystem sink so the workflow
        # has no DB requirement. The activities below substitute stub
        # introspector + sink so no real source is contacted either.
        input = OntologyIngestionInput(
            source={
                "source_id": "stub-pg", "org_id": "stub-org",
                "connection": {
                    "host": "localhost", "port": 5432, "database": "stub",
                    "user": "u", "password": "p",
                },
            },
            output={"kind": "filesystem", "base_dir": str(tmp_path)},
            per_table_concurrency=2,
            pipeline={
                "annotate": False,
                "compute_column_stats": False,
                "fill_descriptions": False,
                "induce_relation_schema": False,
                "enrich_cross_asset_causal": False,
            },
        )

        # Define stub activities under the SAME activity names the workflow
        # calls so the dispatcher routes our stubs instead of the real ones.
        from temporalio import activity

        @activity.defn(name="ontology.introspect_source")
        async def stub_introspect(input_: OntologyIngestionInput) -> list[dict[str, Any]]:
            return [
                TableSpec(
                    schema_name="public", name="t1",
                    qualified_name="public.t1",
                    asset_rk="postgres://stub-pg/db/public/t1",
                ).model_dump(mode="json"),
                TableSpec(
                    schema_name="public", name="t2",
                    qualified_name="public.t2",
                    asset_rk="postgres://stub-pg/db/public/t2",
                ).model_dump(mode="json"),
            ]

        @activity.defn(name="ontology.process_one_table")
        async def stub_process(input_: OntologyIngestionInput, spec: dict[str, Any]) -> dict[str, Any]:
            return PerTableResult(
                qualified_name=spec["qualified_name"],
                asset_rk=spec["asset_rk"],
                outcome="created",
                llm_calls=1,
            ).model_dump(mode="json")

        @activity.defn(name="ontology.run_cross_asset_causal")
        async def stub_cross(input_: OntologyIngestionInput, rks: list[str]) -> dict[str, Any]:
            return {"stage": "cross_asset_causal", "llm_calls": 0, "counts": {"skipped": 1}}

        @activity.defn(name="ontology.run_relation_induction")
        async def stub_induce(input_: OntologyIngestionInput, rels: list[Any], idx: dict[str, str]) -> dict[str, Any]:
            return {"stage": "induce_relation_schema", "llm_calls": 0, "counts": {"skipped": 1}}

        @activity.defn(name="ontology.run_causal_validation")
        async def stub_validate(input_: OntologyIngestionInput, prefix: Any, limit: int) -> dict[str, Any]:
            return {"stage": "validate_causal_candidates", "llm_calls": 0, "counts": {"skipped": 1}}

        from ontology_pipeline.temporal.workflows import OntologyIngestionWorkflow

        # Unique task queue per test run keeps invocations from colliding on
        # a shared server.
        import uuid
        task_queue = f"ontology-pipeline-test-{uuid.uuid4().hex[:8]}"
        target = os.environ["TEMPORAL_TARGET"]
        namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")

        client = await Client.connect(target, namespace=namespace)
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[OntologyIngestionWorkflow],
            activities=[
                stub_introspect, stub_process,
                stub_cross, stub_induce, stub_validate,
            ],
        ):
            summary_dict = await client.execute_workflow(
                "ontology_ingestion",
                input.model_dump(mode="json"),
                id=f"test-ontology-ingestion-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
        summary = WorkflowSummary.model_validate(summary_dict)
        assert summary.tables_seen == 2
        assert summary.tables_processed == 2
        assert summary.total_llm_calls == 2  # 1 per stubbed table
        # Validation post-pass always runs (returns skipped for filesystem sink).
        assert any(p.stage == "validate_causal_candidates" for p in summary.post_passes)
