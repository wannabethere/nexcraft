from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from nexcraft.core.context import QueryContext

if TYPE_CHECKING:
    import duckdb


@dataclass(frozen=True)
class JobContext:
    """Identity, lifecycle, and budgets for one recipe run.

    Matches the contract in jobs/01-recipes.md. Frozen by convention; the
    runtime threads new copies via ``dataclasses.replace`` (or ``attach_duckdb``)
    when it needs to inject the active DuckDB connection for compute().
    """

    job_id: str
    tenant_id: str
    recipe_name: str = ""
    recipe_version: str = ""
    submitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    workflow_id: str = ""

    trace_id: str | None = None

    extract_row_budget: int | None = 50_000_000
    extract_byte_budget: int | None = None
    extract_deadline_per_query: timedelta = timedelta(minutes=10)

    memory_budget: str = "8GB"
    cpu_budget: int = 4
    scratch_dir: str | None = None

    job_deadline: datetime | None = None

    cancel: asyncio.Event = field(default_factory=asyncio.Event, compare=False)

    query: QueryContext | None = None

    _duckdb: "duckdb.DuckDBPyConnection | None" = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id must be non-empty")
        if not self.job_id:
            raise ValueError("job_id must be non-empty")

    def attach_duckdb(self, con: "duckdb.DuckDBPyConnection") -> "JobContext":
        """Return a copy of this context with the live DuckDB connection attached.

        The runtime calls this just before invoking ``recipe.compute`` so the
        recipe can reach the pre-configured connection via ``ctx._duckdb`` while
        the rest of the context (cancel event, deadlines, budgets) is shared.
        """
        return replace(self, _duckdb=con)

    def derive_query_context(self, query_id: str) -> QueryContext:
        """Build a QueryContext for an extract sub-query off this job's budgets.

        Wires job-level budgets and tracing into a fresh QueryContext. Recipes
        usually call this from ``extract`` to get a context that propagates the
        job's row/byte budgets and per-query deadline into nexcraft.
        """
        deadline: datetime | None
        if self.extract_deadline_per_query is not None:
            deadline = datetime.now(timezone.utc) + self.extract_deadline_per_query
            if self.job_deadline is not None and self.job_deadline < deadline:
                deadline = self.job_deadline
        else:
            deadline = self.job_deadline

        return QueryContext(
            tenant_id=self.tenant_id,
            query_id=query_id,
            trace_id=self.trace_id,
            deadline=deadline,
            max_rows=self.extract_row_budget,
            max_bytes=self.extract_byte_budget,
        )


@dataclass(frozen=True)
class JobContextSnapshot:
    """Temporal-safe subset (no asyncio.Event, no DuckDB conn). Use ``rehydrate``."""

    tenant_id: str
    job_id: str
    recipe_name: str = ""
    recipe_version: str = ""
    submitted_at: datetime | None = None
    workflow_id: str = ""

    trace_id: str | None = None

    extract_row_budget: int | None = 50_000_000
    extract_byte_budget: int | None = None
    extract_deadline_seconds: float = 600.0

    memory_budget: str = "8GB"
    cpu_budget: int = 4
    scratch_dir: str | None = None

    job_deadline: datetime | None = None

    def rehydrate(self) -> JobContext:
        return JobContext(
            tenant_id=self.tenant_id,
            job_id=self.job_id,
            recipe_name=self.recipe_name,
            recipe_version=self.recipe_version,
            submitted_at=self.submitted_at or datetime.now(timezone.utc),
            workflow_id=self.workflow_id,
            trace_id=self.trace_id,
            extract_row_budget=self.extract_row_budget,
            extract_byte_budget=self.extract_byte_budget,
            extract_deadline_per_query=timedelta(seconds=self.extract_deadline_seconds),
            memory_budget=self.memory_budget,
            cpu_budget=self.cpu_budget,
            scratch_dir=self.scratch_dir,
            job_deadline=self.job_deadline,
        )


def snapshot_job_context(ctx: JobContext) -> JobContextSnapshot:
    return JobContextSnapshot(
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        recipe_name=ctx.recipe_name,
        recipe_version=ctx.recipe_version,
        submitted_at=ctx.submitted_at,
        workflow_id=ctx.workflow_id,
        trace_id=ctx.trace_id,
        extract_row_budget=ctx.extract_row_budget,
        extract_byte_budget=ctx.extract_byte_budget,
        extract_deadline_seconds=ctx.extract_deadline_per_query.total_seconds(),
        memory_budget=ctx.memory_budget,
        cpu_budget=ctx.cpu_budget,
        scratch_dir=ctx.scratch_dir,
        job_deadline=ctx.job_deadline,
    )


__all__ = [
    "JobContext",
    "JobContextSnapshot",
    "snapshot_job_context",
]
