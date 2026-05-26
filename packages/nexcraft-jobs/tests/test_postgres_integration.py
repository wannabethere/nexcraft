"""Postgres integration tests against the Cornerstone Learning schema.

Skipped when env vars are missing — `pytest tests/` on a laptop without
credentials stays green. To run locally:

    set -a; source .env; set +a
    pytest tests/test_postgres_integration.py -v
"""
from __future__ import annotations

import os

import pandas as pd
import pytest

from nexcraft_jobs.compute.postgres_runner import (
    credentials_available,
    get_postgres_connection,
    materialize_sample,
    render_only_postgres,
    run_python_tool_postgres,
    run_sql_tool_postgres,
)

_NEEDS = {
    "POSTGRES_HOST", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD",
    "CORNERSTONE_TABLE", "CORNERSTONE_SCORE_COL", "CORNERSTONE_TIME_COL",
    "CORNERSTONE_STATUS_COL", "CORNERSTONE_USER_COL", "CORNERSTONE_ACTIVITY_COL",
}
pytestmark = pytest.mark.skipif(
    not credentials_available() or any(not os.environ.get(k) for k in _NEEDS),
    reason="Postgres / Cornerstone env vars not set; see .env.example.",
)


@pytest.fixture(scope="module")
def con():
    c = get_postgres_connection()
    yield c
    c.close()


@pytest.fixture(scope="module")
def cfg(con) -> dict[str, str]:
    sample = materialize_sample(
        con,
        source_table=os.environ["CORNERSTONE_TABLE"],
        sample_name="cornerstone_sample",
        n_rows=int(os.environ.get("CORNERSTONE_SAMPLE_ROWS", "50000")),
    )
    return {
        "table":    sample,
        "score":    os.environ["CORNERSTONE_SCORE_COL"],
        "time":     os.environ["CORNERSTONE_TIME_COL"],
        "status":   os.environ["CORNERSTONE_STATUS_COL"],
        "user":     os.environ["CORNERSTONE_USER_COL"],
        "activity": os.environ["CORNERSTONE_ACTIVITY_COL"],
    }


# --- Render-only checks ------------------------------------------------------


def test_distribution_summary_renders_to_postgres(cfg) -> None:
    sql = render_only_postgres("distribution_summary", {
        "table": cfg["table"], "value_col": cfg["score"],
        "group_cols": [cfg["status"]],
    })
    assert "PERCENTILE_CONT" in sql.upper()


def test_flux_variance_renders_to_postgres(cfg) -> None:
    sql = render_only_postgres("flux_variance", {
        "table":         cfg["table"], "amount_col": cfg["score"],
        "date_col":      cfg["time"], "dimensions": [cfg["status"]],
        "filter_clause": "TRUE", "material_pct": 0.20, "grain": "month",
    })
    assert "LAG" in sql.upper() and "DATE_TRUNC" in sql.upper()


# --- Execution checks --------------------------------------------------------


def test_distribution_summary_runs(con, cfg) -> None:
    df = run_sql_tool_postgres(con, "distribution_summary", {
        "table":      cfg["table"], "value_col": cfg["score"],
        "group_cols": [cfg["status"]],
    })
    assert {"n", "mean", "p50", "p95"}.issubset(df.columns)
    assert len(df) > 0


def test_flux_variance_runs(con, cfg) -> None:
    df = run_sql_tool_postgres(con, "flux_variance", {
        "table":         cfg["table"], "amount_col": cfg["score"],
        "date_col":      cfg["time"], "dimensions": [cfg["status"]],
        "filter_clause": f'{cfg["time"]} IS NOT NULL',
        "material_pct":  0.20, "grain": "month",
    })
    assert {"period", "amount", "flux_label"}.issubset(df.columns)
    assert set(df["flux_label"].dropna().unique()) <= {"new", "stable", "material", "dropped"}


def test_statistical_trend_runs(con, cfg) -> None:
    df = run_sql_tool_postgres(con, "statistical_trend", {
        "table":      cfg["table"], "value_col": cfg["score"],
        "time_col":   cfg["time"], "group_cols": [cfg["activity"]],
        "grain":      "month",
    })
    assert {"slope", "intercept", "r_squared", "n_periods"}.issubset(df.columns)


def test_outliers_iqr_runs(con, cfg) -> None:
    sql = render_only_postgres("outliers_iqr", {
        "table": cfg["table"], "value_col": cfg["score"],
        "group_cols": [cfg["activity"]], "iqr_multiplier": 3.0,
    })
    cur = con.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) AS n, SUM(is_outlier) AS n_out "
                    f"FROM ({sql.rstrip(';')}) sub")
        n, n_out = cur.fetchone()
    finally:
        cur.close()
    assert int(n) > 0
    assert int(n_out) >= 0


def test_python_lane_historical_var_on_sample(con, cfg) -> None:
    out = run_python_tool_postgres(
        con,
        query=f'SELECT {cfg["score"]} AS score FROM "{cfg["table"]}" LIMIT 5000',
        name="historical_var_pd",
        params={"returns_col": "score", "alpha": 0.05},
    )
    df = pd.DataFrame(out.data) if hasattr(out, "data") else out
    assert {"var", "alpha"}.issubset(df.columns)
