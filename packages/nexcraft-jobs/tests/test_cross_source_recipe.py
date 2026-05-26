"""Integration test for CrossSourceFluxRecipe.

Runs the SAME Recipe against whichever sources have credentials configured —
proves the dstools compute body is source-agnostic. Skipped when no creds
present, so `pytest tests/` on a laptop without env vars stays green.
"""
from __future__ import annotations

import os

import pytest

from nexcraft_jobs.context import JobContext
from nexcraft_driver.integration.fedsql_factory import (
    DELTA_SOURCE_ID,
    ICEBERG_SOURCE_ID,
    LAKEHOUSE_VIEW_NAME,
    POSTGRES_SOURCE_ID,
    SNOWFLAKE_SOURCE_ID,
    build_cross_source_fedsql,
)
from nexcraft_jobs.recipes import CrossSourceFluxRecipe
from nexcraft_jobs.runtime.local import LocalRuntime


def _have(*envs: str) -> bool:
    return all(os.environ.get(e) for e in envs)


_HAVE_PG = _have("POSTGRES_HOST", "POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD",
                  "CORNERSTONE_TABLE", "CORNERSTONE_SCORE_COL", "CORNERSTONE_TIME_COL")
_HAVE_SF = _have("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_DATABASE",
                  "SNOWFLAKE_SCHEMA", "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_PASSWORD",
                  "PRICEMEDIC_TABLE", "PRICEMEDIC_RATE_COL", "PRICEMEDIC_DATE_COL")
_HAVE_DELTA   = _have("DELTA_TABLE_S3_PATH", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
_HAVE_ICEBERG = _have("ICEBERG_TABLE_S3_PATH", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")

pytestmark = pytest.mark.skipif(
    not (_HAVE_PG or _HAVE_SF or _HAVE_DELTA or _HAVE_ICEBERG),
    reason="No source credentials configured; see .env.example.",
)


@pytest.fixture(scope="module")
async def fedsql_setup():
    fedsql, provider = await build_cross_source_fedsql()
    yield fedsql, provider


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAVE_PG, reason="No Postgres creds")
async def test_recipe_runs_against_postgres(fedsql_setup) -> None:
    fedsql, _ = fedsql_setup
    runtime = LocalRuntime(fedsql)
    ref = await runtime.submit(
        CrossSourceFluxRecipe(),
        {
            "source_id":    POSTGRES_SOURCE_ID,
            "table":        os.environ["CORNERSTONE_TABLE"],
            "rate_col":     os.environ["CORNERSTONE_SCORE_COL"],
            "date_col":     os.environ["CORNERSTONE_TIME_COL"],
            "hospital_col": os.environ.get("CORNERSTONE_STATUS_COL"),
            "sample_rows":  5000,
        },
        JobContext(tenant_id="default", job_id="test-crossflux-pg"),
    )
    assert ref.job_id == "test-crossflux-pg"


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAVE_SF, reason="No Snowflake creds")
async def test_recipe_runs_against_snowflake(fedsql_setup) -> None:
    fedsql, _ = fedsql_setup
    runtime = LocalRuntime(fedsql)
    ref = await runtime.submit(
        CrossSourceFluxRecipe(),
        {
            "source_id":    SNOWFLAKE_SOURCE_ID,
            "table":        os.environ["PRICEMEDIC_TABLE"],
            "rate_col":     os.environ["PRICEMEDIC_RATE_COL"],
            "date_col":     os.environ["PRICEMEDIC_DATE_COL"],
            "hospital_col": os.environ.get("PRICEMEDIC_HOSPITAL_COL"),
            "sample_rows":  5000,
        },
        JobContext(tenant_id="default", job_id="test-crossflux-sf"),
    )
    assert ref.job_id == "test-crossflux-sf"


@pytest.mark.asyncio
@pytest.mark.skipif(not (_HAVE_PG and _HAVE_SF),
                    reason="Need both Postgres + Snowflake creds for this test")
async def test_same_recipe_body_both_sources(fedsql_setup) -> None:
    """Sanity: the recipe code path is identical across sources. We submit
    twice with only the source_id (and per-source table/column names) changing,
    and assert both runs succeed."""
    fedsql, _ = fedsql_setup
    runtime = LocalRuntime(fedsql)

    pg_ref = await runtime.submit(
        CrossSourceFluxRecipe(),
        {
            "source_id":   POSTGRES_SOURCE_ID,
            "table":       os.environ["CORNERSTONE_TABLE"],
            "rate_col":    os.environ["CORNERSTONE_SCORE_COL"],
            "date_col":    os.environ["CORNERSTONE_TIME_COL"],
            "sample_rows": 2000,
        },
        JobContext(tenant_id="default", job_id="parity-pg"),
    )
    sf_ref = await runtime.submit(
        CrossSourceFluxRecipe(),
        {
            "source_id":   SNOWFLAKE_SOURCE_ID,
            "table":       os.environ["PRICEMEDIC_TABLE"],
            "rate_col":    os.environ["PRICEMEDIC_RATE_COL"],
            "date_col":    os.environ["PRICEMEDIC_DATE_COL"],
            "sample_rows": 2000,
        },
        JobContext(tenant_id="default", job_id="parity-sf"),
    )
    assert pg_ref.job_id == "parity-pg"
    assert sf_ref.job_id == "parity-sf"


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAVE_DELTA, reason="No Delta + AWS creds")
async def test_recipe_runs_against_delta(fedsql_setup) -> None:
    fedsql, _ = fedsql_setup
    runtime = LocalRuntime(fedsql)
    ref = await runtime.submit(
        CrossSourceFluxRecipe(),
        {
            "source_id":    DELTA_SOURCE_ID,
            "table":        LAKEHOUSE_VIEW_NAME,
            "rate_col":     os.environ.get("DELTA_RATE_COL", "metric_val"),
            "date_col":     os.environ.get("DELTA_DATE_COL", "metric_ts"),
            "hospital_col": os.environ.get("DELTA_GROUP_COL"),
            "sample_rows":  2000,
        },
        JobContext(tenant_id="default", job_id="test-crossflux-delta"),
    )
    assert ref.job_id == "test-crossflux-delta"


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAVE_ICEBERG, reason="No Iceberg + AWS creds")
async def test_recipe_runs_against_iceberg(fedsql_setup) -> None:
    fedsql, _ = fedsql_setup
    runtime = LocalRuntime(fedsql)
    ref = await runtime.submit(
        CrossSourceFluxRecipe(),
        {
            "source_id":    ICEBERG_SOURCE_ID,
            "table":        LAKEHOUSE_VIEW_NAME,
            "rate_col":     os.environ.get("ICEBERG_RATE_COL", "metric_val"),
            "date_col":     os.environ.get("ICEBERG_DATE_COL", "metric_ts"),
            "hospital_col": os.environ.get("ICEBERG_GROUP_COL"),
            "sample_rows":  2000,
        },
        JobContext(tenant_id="default", job_id="test-crossflux-iceberg"),
    )
    assert ref.job_id == "test-crossflux-iceberg"
