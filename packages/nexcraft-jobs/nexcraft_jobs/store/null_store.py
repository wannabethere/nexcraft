from __future__ import annotations

from typing import Any, Mapping

from nexcraft_jobs.context import JobContext
from nexcraft_jobs.types import ComputeResult, ResultRef


class NullResultStore:
    """No-op store: returns a memory:// URI without writing anything.

    Useful for tests and unit recipes where the persisted artifact is not the
    point of the test.
    """

    async def finalize(
        self,
        ctx: JobContext,
        result: ComputeResult,
        params: Mapping[str, Any],
    ) -> ResultRef:
        return ResultRef(uri=f"memory://noop/{ctx.job_id}", job_id=ctx.job_id)
