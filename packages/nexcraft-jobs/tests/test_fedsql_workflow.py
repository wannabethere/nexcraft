"""Tests for nexcraft_fedsql_query workflow schemas and registry."""
from __future__ import annotations

import pyarrow as pa
import pytest

from nexcraft_jobs.runtime.workflow_type_registry import (
    get_workflow_input_model,
    registered_workflow_types,
    validate_workflow_input,
)
from nexcraft_jobs.schemas import ExecutionResult, FedSQLQueryInput, GeniemlPostOutputConfig


def test_fedsql_query_input_resolves_source_id() -> None:
    inp = FedSQLQueryInput(sql="SELECT 1", source_id="preview")
    assert inp.resolved_source_id() == "preview"


def test_workflow_type_registry_includes_fedsql() -> None:
    assert "nexcraft_fedsql_query" in registered_workflow_types()
    model = get_workflow_input_model("nexcraft_fedsql_query")
    assert model is FedSQLQueryInput


def test_fedsql_input_accepts_post_output() -> None:
    inp = FedSQLQueryInput(
        sql="SELECT 1",
        source_id="preview",
        post_output=GeniemlPostOutputConfig(
            question="Q?",
            sql="SELECT 1",
            summarize=True,
            chart=True,
        ),
    )
    assert inp.post_output is not None
    assert inp.post_output.chart is True


def test_validate_workflow_input_fedsql() -> None:
    validated = validate_workflow_input(
        "nexcraft_fedsql_query",
        {"sql": "SELECT 1", "source_id": "x", "tenant_id": "t1"},
    )
    assert isinstance(validated, FedSQLQueryInput)


def test_execution_result_roundtrip() -> None:
    from nexcraft_jobs.schemas import ColumnSpec

    er = ExecutionResult(
        rows=[{"a": 1}],
        columns=[ColumnSpec(name="a", type="int64")],
        row_count=1,
        exec_time_ms=10,
    )
    data = er.model_dump()
    assert ExecutionResult.model_validate(data).row_count == 1


@pytest.mark.asyncio
async def test_fedsql_activity_with_mock_client(monkeypatch) -> None:
    from nexcraft_jobs.runtime import fedsql_activities
    from nexcraft_jobs.runtime.worker_config import configure_worker, reset_worker_config

    class _FakeClient:
        async def execute_to_table(self, source_id: str, sql: str, ctx) -> pa.Table:
            return pa.table({"n": [1, 2, 3]})

    configure_worker(fedsql=_FakeClient())  # type: ignore[arg-type]
    try:
        out = await fedsql_activities.fedsql_execute_to_dataframe(
            FedSQLQueryInput(sql="SELECT 1", source_id="preview", row_limit=2)
        )
        assert out.row_count == 2
        assert len(out.rows) == 2
        assert out.columns[0].name == "n"
    finally:
        reset_worker_config()
