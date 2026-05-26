from __future__ import annotations

from datetime import timedelta

from nexcraft_jobs.context import JobContext, snapshot_job_context


def test_derive_query_context_propagates_budgets() -> None:
    ctx = JobContext(
        tenant_id="t1",
        job_id="job-1",
        recipe_name="r",
        recipe_version="1.0.0",
        extract_row_budget=1_000_000,
        extract_byte_budget=10_000_000,
        extract_deadline_per_query=timedelta(seconds=30),
        trace_id="trace-abc",
    )
    qc = ctx.derive_query_context("q-1")
    assert qc.tenant_id == "t1"
    assert qc.query_id == "q-1"
    assert qc.trace_id == "trace-abc"
    assert qc.max_rows == 1_000_000
    assert qc.max_bytes == 10_000_000
    assert qc.deadline is not None


def test_attach_duckdb_returns_new_instance_with_conn() -> None:
    ctx = JobContext(tenant_id="t", job_id="j")
    sentinel = object()
    new_ctx = ctx.attach_duckdb(sentinel)  # type: ignore[arg-type]
    assert new_ctx is not ctx
    assert new_ctx._duckdb is sentinel
    assert ctx._duckdb is None
    # Identity fields preserved
    assert new_ctx.tenant_id == "t" and new_ctx.job_id == "j"


def test_snapshot_round_trip() -> None:
    ctx = JobContext(
        tenant_id="t",
        job_id="j",
        recipe_name="rn",
        recipe_version="1.2.3",
        memory_budget="2GB",
        cpu_budget=2,
        scratch_dir="/tmp/x",
        extract_row_budget=42,
    )
    snap = snapshot_job_context(ctx)
    rehydrated = snap.rehydrate()
    assert rehydrated.tenant_id == "t"
    assert rehydrated.recipe_name == "rn"
    assert rehydrated.recipe_version == "1.2.3"
    assert rehydrated.memory_budget == "2GB"
    assert rehydrated.cpu_budget == 2
    assert rehydrated.scratch_dir == "/tmp/x"
    assert rehydrated.extract_row_budget == 42
