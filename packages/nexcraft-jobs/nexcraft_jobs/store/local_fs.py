"""Reference ResultStore that writes Parquet + JSON metadata to a local directory."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pyarrow.parquet as pq

from nexcraft_jobs.context import JobContext
from nexcraft_jobs.types import ComputeResult, ResultRef


class LocalFsResultStore:
    """Persist a ComputeResult under ``<root>/<tenant>/<job_id>/``.

    Layout::

        <root>/<tenant>/<job_id>/primary.parquet
                                 auxiliaries/<name>.parquet (if any)
                                 metadata.json

    The returned ResultRef points at the job directory as a ``file://`` URI.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    async def finalize(
        self,
        ctx: JobContext,
        result: ComputeResult,
        params: Mapping[str, Any],
    ) -> ResultRef:
        job_dir = self._root / ctx.tenant_id / ctx.job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        primary_path = job_dir / "primary.parquet"
        pq.write_table(result.primary, primary_path, compression="zstd", compression_level=3)

        if result.auxiliaries:
            aux_dir = job_dir / "auxiliaries"
            aux_dir.mkdir(parents=True, exist_ok=True)
            for name, table in result.auxiliaries.items():
                safe = name.replace("/", "_").replace("..", "")
                pq.write_table(
                    table,
                    aux_dir / f"{safe}.parquet",
                    compression="zstd",
                    compression_level=3,
                )

        manifest = {
            "job_id": ctx.job_id,
            "tenant_id": ctx.tenant_id,
            "recipe_name": ctx.recipe_name,
            "recipe_version": ctx.recipe_version,
            "workflow_id": ctx.workflow_id,
            "submitted_at": ctx.submitted_at.isoformat() if ctx.submitted_at else None,
            "finalized_at": datetime.now(timezone.utc).isoformat(),
            "primary_rows": result.primary.num_rows,
            "auxiliaries": sorted((result.auxiliaries or {}).keys()),
            "metadata": result.metadata or {},
            "params": dict(params),
        }
        (job_dir / "metadata.json").write_text(json.dumps(manifest, default=str, indent=2))

        return ResultRef(uri=job_dir.resolve().as_uri(), job_id=ctx.job_id)
