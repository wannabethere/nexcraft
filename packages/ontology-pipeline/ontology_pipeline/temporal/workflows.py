"""OntologyIngestionWorkflow — the Temporal entry point for nexcraft-jobs.

Shape (matches `nexcraft_jobs.yaml_jobs.spec.TemporalJobSpec`):

    workflow_type: ontology_ingestion
    task_queue:    ontology-pipeline-default
    input:         <OntologyIngestionInput as dict>

Flow:

    1. introspect_source              → list[TableSpec]
    2. fan-out per-table activities   → list[PerTableResult]      (bounded by `per_table_concurrency`)
    3. accumulate relationships + concepts from per-table results
    4. run_cross_asset_causal         → PostPassResult            (if enabled, ≥2 built)
    5. run_relation_induction         → PostPassResult            (if enabled, edges accumulated)
    6. run_causal_validation          → PostPassResult            (if hierarchy_store/tee sink)
    7. return WorkflowSummary

Determinism contract (Temporal):
  - No DB / network calls in the workflow body. Everything that touches the
    outside world is in an activity.
  - Time / random / IO are reached via `workflow.now()`, `workflow.random()`,
    etc. — not the host runtime — so replay is deterministic. (We don't
    currently need any of those, but the constraint is the reason.)

Module loading: when `temporalio` is unavailable the workflow class is set
to None so importers can still introspect the input shapes. The Temporal
SDK requires `@workflow.run` on a module-scope class — defining the class
inside a function fails at decoration time — so the guard runs at the
TOP level of this module.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

# Import the input shapes unconditionally — pure Pydantic, no temporalio.
from ontology_pipeline.temporal.inputs import (
    OntologyIngestionInput,
    PerTableResult,
    PostPassResult,
    WorkflowSummary,
)


try:
    from temporalio import workflow
    from temporalio.common import RetryPolicy
    _HAS_TEMPORAL = True
except ImportError:
    _HAS_TEMPORAL = False


if _HAS_TEMPORAL:

    @workflow.defn(name="ontology_ingestion")
    class OntologyIngestionWorkflow:
        """Orchestrates the per-table fan-out + post-passes."""

        @workflow.run
        async def run(self, input_dict: dict[str, Any]) -> dict[str, Any]:
            # Temporal serialises in/out as JSON-compatible dicts. We validate
            # inside the workflow so callers don't have to construct Pydantic
            # models manually.
            input = OntologyIngestionInput.model_validate(input_dict)

            # ── Step 1: introspect ──────────────────────────────────────
            table_specs = await workflow.execute_activity(
                "ontology.introspect_source",
                input,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=5),
                    backoff_coefficient=2.0,
                    maximum_attempts=3,
                ),
            )
            workflow.logger.info("introspect found %d tables", len(table_specs))

            # ── Step 2: per-table fan-out ───────────────────────────────
            results = await self._fan_out_tables(
                input=input, table_specs=table_specs,
            )

            # ── Step 3: accumulate cross-table state ────────────────────
            relationships: list[dict[str, Any]] = []
            concept_index: dict[str, str] = {}
            built_asset_rks: list[str] = []
            tables_seen = len(results)
            tables_processed = 0
            tables_skipped_unchanged = 0
            tables_errored = 0
            total_llm_calls = 0
            for r in results:
                pr = PerTableResult.model_validate(r)
                total_llm_calls += pr.llm_calls
                if pr.outcome == "unchanged":
                    tables_skipped_unchanged += 1
                elif pr.outcome == "error":
                    tables_errored += 1
                else:
                    tables_processed += 1
                    built_asset_rks.append(pr.asset_rk)
                if pr.primary_concept:
                    concept_index[pr.asset_rk] = pr.primary_concept
                relationships.extend(pr.inferred_relationships)

            # ── Steps 4–6: post-passes (fixed order) ────────────────────
            post_passes: list[dict[str, Any]] = []

            if input.pipeline.enrich_cross_asset_causal and len(built_asset_rks) >= 2:
                cross = await workflow.execute_activity(
                    "ontology.run_cross_asset_causal",
                    args=[input, built_asset_rks],
                    start_to_close_timeout=timedelta(minutes=30),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
                cross_pp = PostPassResult.model_validate(cross)
                total_llm_calls += cross_pp.llm_calls
                post_passes.append(cross)

            if input.pipeline.induce_relation_schema and relationships:
                ind = await workflow.execute_activity(
                    "ontology.run_relation_induction",
                    args=[input, relationships, concept_index],
                    start_to_close_timeout=timedelta(minutes=15),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
                ind_pp = PostPassResult.model_validate(ind)
                total_llm_calls += ind_pp.llm_calls
                post_passes.append(ind)

            # Causal validation always runs when the sink supports it — the
            # activity itself short-circuits otherwise.
            val = await workflow.execute_activity(
                "ontology.run_causal_validation",
                args=[input, None, 200],
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            post_passes.append(val)

            # ── Step 7: terminal summary ────────────────────────────────
            summary = WorkflowSummary(
                source_id=input.source.source_id,
                tables_seen=tables_seen,
                tables_processed=tables_processed,
                tables_skipped_unchanged=tables_skipped_unchanged,
                tables_errored=tables_errored,
                total_llm_calls=total_llm_calls,
                post_passes=[PostPassResult.model_validate(p) for p in post_passes],
                per_table=[PerTableResult.model_validate(r) for r in results],
            )
            workflow.logger.info(
                "workflow complete: seen=%d processed=%d unchanged=%d errored=%d llm_calls=%d",
                summary.tables_seen, summary.tables_processed,
                summary.tables_skipped_unchanged, summary.tables_errored,
                summary.total_llm_calls,
            )
            return summary.model_dump(mode="json")

        async def _fan_out_tables(
            self,
            *,
            input: OntologyIngestionInput,
            table_specs: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            """Run per-table activities concurrently, capped by `per_table_concurrency`.

            Each table gets its own retry policy — a 503 from an LLM mid-table
            doesn't abort the workflow, just that table's activity. After
            max_attempts a table comes back with `outcome='error'` in its result.
            """
            sem = asyncio.Semaphore(input.per_table_concurrency)

            async def _one(spec_dict: dict[str, Any]) -> dict[str, Any]:
                async with sem:
                    return await workflow.execute_activity(
                        "ontology.process_one_table",
                        args=[input, spec_dict],
                        start_to_close_timeout=timedelta(minutes=20),
                        retry_policy=RetryPolicy(
                            initial_interval=timedelta(seconds=5),
                            backoff_coefficient=2.0,
                            maximum_attempts=3,
                        ),
                    )

            return await asyncio.gather(*[_one(s) for s in table_specs])

else:
    OntologyIngestionWorkflow = None  # type: ignore[assignment,misc]
