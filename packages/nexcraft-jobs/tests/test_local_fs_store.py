from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from nexcraft_jobs.context import JobContext
from nexcraft_jobs.store.local_fs import LocalFsResultStore
from nexcraft_jobs.types import ComputeResult


@pytest.mark.asyncio
async def test_local_fs_store_writes_primary_aux_metadata(tmp_path: Path) -> None:
    store = LocalFsResultStore(tmp_path)
    ctx = JobContext(
        tenant_id="t1",
        job_id="job-42",
        recipe_name="r",
        recipe_version="1.0.0",
        submitted_at=datetime.now(timezone.utc),
    )
    primary = pa.table({"region": ["us", "eu"], "amount": [100, 200]})
    auxiliaries = {"by_region": pa.table({"region": ["us", "eu"], "n": [1, 1]})}
    result = ComputeResult(
        primary=primary,
        auxiliaries=auxiliaries,
        metadata={"row_count": 2, "regions": ["us", "eu"]},
    )

    ref = await store.finalize(ctx, result, {"period": "2026Q1"})

    job_dir = tmp_path / "t1" / "job-42"
    assert job_dir.is_dir()
    assert pq.read_table(job_dir / "primary.parquet").equals(primary)
    assert pq.read_table(job_dir / "auxiliaries" / "by_region.parquet").equals(
        auxiliaries["by_region"]
    )
    manifest = json.loads((job_dir / "metadata.json").read_text())
    assert manifest["job_id"] == "job-42"
    assert manifest["primary_rows"] == 2
    assert manifest["auxiliaries"] == ["by_region"]
    assert manifest["metadata"]["regions"] == ["us", "eu"]
    assert ref.uri.startswith("file:")
    assert ref.job_id == "job-42"
