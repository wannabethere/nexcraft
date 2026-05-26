"""High-level orchestrator that runs nexcraft recipes on Temporal.

Mirrors the entry-point shown in jobs/01-recipes.md: register a set of recipes,
spin up a worker that handles the validate/extract/compute/persist activities
for that task queue, and provide a ``submit`` helper for callers that want to
kick off a workflow without dealing with the Temporal client directly.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from nexcraft.client import FedSQLClient

from nexcraft_jobs.recipe import Recipe, ResultStore
from nexcraft_jobs.runtime.recipe_staged_workflow import RecipeStagedWorkflow
from nexcraft_jobs.runtime.registry import GLOBAL_REGISTRY, RecipeRegistry
from nexcraft_jobs.runtime.temporal_payloads import SubmitJobPayload
from nexcraft_jobs.runtime.temporal_worker_bundle import (
    NEXCRAFT_RECIPE_ACTIVITIES,
    NEXCRAFT_RECIPE_WORKFLOWS,
)
from nexcraft_jobs.runtime.temporal_workflows import RecipeInlineWorkflow
from nexcraft_jobs.runtime.worker_config import configure_worker
from nexcraft_jobs.types import ResultRef


class TemporalRuntime:
    """Wires recipes into a Temporal worker and exposes a typed submission API.

    Usage::

        runtime = TemporalRuntime(
            temporal_target="temporal:7233",
            namespace="default",
            task_queue="nexcraft-jobs",
            fedsql_client=client,
            result_store=store,
            recipes=[VarianceAnalysisRecipe(), TrendAnalysisRecipe()],
            staging_root="/var/lib/nexcraft/staging",
        )
        await runtime.start()
        ref = await runtime.submit("variance_analysis", "1.0.0", params={...})

    The class lazily imports ``temporalio`` so importing nexcraft_jobs without
    a running Temporal server stays cheap.
    """

    def __init__(
        self,
        *,
        temporal_target: str,
        namespace: str,
        task_queue: str,
        fedsql_client: FedSQLClient,
        result_store: ResultStore,
        recipes: Iterable[Recipe] = (),
        staging_root: str | None = None,
        registry: RecipeRegistry | None = None,
        extra_workflows: Sequence[Any] = (),
        extra_activities: Sequence[Any] = (),
    ) -> None:
        self._temporal_target = temporal_target
        self._namespace = namespace
        self._task_queue = task_queue
        self._fedsql_client = fedsql_client
        self._result_store = result_store
        self._staging_root = staging_root
        self._registry = registry or GLOBAL_REGISTRY
        self._extra_workflows = list(extra_workflows)
        self._extra_activities = list(extra_activities)

        for recipe in recipes:
            try:
                self._registry.register(recipe)
            except ValueError:
                # Re-registration of the same recipe (e.g. across hot-reloads)
                # should be idempotent, not fatal.
                pass

        self._client = None
        self._worker = None
        self._worker_task = None

    async def start(self) -> None:
        """Connect to Temporal and start the worker for this task queue."""
        from temporalio.client import Client
        from temporalio.worker import Worker

        configure_worker(fedsql=self._fedsql_client, store=self._result_store)

        self._client = await Client.connect(
            self._temporal_target, namespace=self._namespace
        )

        workflows = [*NEXCRAFT_RECIPE_WORKFLOWS, *self._extra_workflows]
        activities = [*NEXCRAFT_RECIPE_ACTIVITIES, *self._extra_activities]

        self._worker = Worker(
            self._client,
            task_queue=self._task_queue,
            workflows=workflows,
            activities=activities,
        )

        import asyncio

        self._worker_task = asyncio.create_task(self._worker.run())

    async def shutdown(self) -> None:
        if self._worker is not None:
            await self._worker.shutdown()
        if self._worker_task is not None:
            try:
                await self._worker_task
            except Exception:
                pass

    async def submit(
        self,
        recipe_name: str,
        recipe_version: str,
        *,
        params: Mapping[str, Any],
        tenant_id: str = "default",
        job_id: str | None = None,
        workflow_id: str | None = None,
        staged: bool = True,
        memory_budget: str = "8GB",
        cpu_budget: int = 4,
        scratch_dir: str | None = None,
        extract_row_budget: int | None = 50_000_000,
        extract_byte_budget: int | None = None,
        extract_deadline: timedelta = timedelta(minutes=10),
        job_deadline: datetime | None = None,
        trace_id: str | None = None,
    ) -> ResultRef:
        """Start a recipe workflow and wait for the resulting ResultRef."""
        if self._client is None:
            raise RuntimeError("TemporalRuntime.start() must be called before submit()")

        # Validate that the recipe is known to this runtime before we hit Temporal.
        self._registry.get(recipe_name, recipe_version)

        job_id = job_id or str(uuid.uuid4())
        workflow_id = workflow_id or f"{recipe_name}:{recipe_version}:{job_id}"

        if staged and not self._staging_root:
            raise ValueError(
                "TemporalRuntime was constructed without staging_root; "
                "either pass staging_root or use staged=False."
            )

        payload = SubmitJobPayload(
            recipe_name=recipe_name,
            recipe_version=recipe_version,
            params=dict(params),
            tenant_id=tenant_id,
            job_id=job_id,
            query_id=job_id,
            workflow_id=workflow_id,
            trace_id=trace_id,
            submitted_at=datetime.now(timezone.utc),
            extract_row_budget=extract_row_budget,
            extract_byte_budget=extract_byte_budget,
            extract_deadline_seconds=extract_deadline.total_seconds(),
            memory_budget=memory_budget,
            cpu_budget=cpu_budget,
            scratch_dir=scratch_dir,
            job_deadline=job_deadline,
            staging_root=self._staging_root if staged else None,
        )

        wf = RecipeStagedWorkflow.run if staged else RecipeInlineWorkflow.run
        handle = await self._client.start_workflow(
            wf,
            payload,
            id=workflow_id,
            task_queue=self._task_queue,
        )
        return await handle.result()


__all__ = ["TemporalRuntime"]
