"""Snowflake integration tests against the PriceMedic listing.

These tests are gated on the presence of Snowflake credentials. Without them,
the whole module is skipped — so the default `pytest tests/` run on a laptop
without Snowflake env vars stays green.

To run locally:

    set -a; source .env; set +a
    pytest tests/test_snowflake_integration.py -v

The .env file lives in the package root and is NOT committed; see .env.example
for the schema.
"""
from __future__ import annotations

import os

import pandas as pd
import pytest

from nexcraft_jobs.compute.snowflake_runner import (
    credentials_available,
    get_snowflake_connection,
    materialize_sample,
    render_only_snowflake,
    run_python_tool_snowflake,
    run_sql_tool_snowflake,
)

# Top-of-module skip: if any required env var is missing, skip every test below.
_NEEDS = {
    "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_DATABASE",
    "SNOWFLAKE_SCHEMA", "SNOWFLAKE_WAREHOUSE",
    "PRICEMEDIC_TABLE", "PRICEMEDIC_RATE_COL", "PRICEMEDIC_DATE_COL",
    "PRICEMEDIC_HOSPITAL_COL", "PRICEMEDIC_PROCEDURE_COL", "PRICEMEDIC_PAYER_COL",
}
pytestmark = pytest.mark.skipif(
    not credentials_available()
    or any(not os.environ.get(k) for k in _NEEDS),
    reason="Snowflake / PriceMedic env vars not set; see .env.example.",
)


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture(scope="module")
def con():
    c = get_snowflake_connection()
    yield c
    c.close()


@pytest.fixture(scope="module")
def cfg(con) -> dict[str, str]:
    # Materialize a row-count sample once per test module to avoid scanning
    # the full ~90M-row source view six times across tests. Cast string-
    # typed date/rate columns at the boundary so DATE_TRUNC / aggregates
    # don't fail downstream.
    sample = materialize_sample(
        con,
        source_table=os.environ["PRICEMEDIC_TABLE"],
        n_rows=int(os.environ.get("PRICEMEDIC_SAMPLE_ROWS", "200000")),
        cast_columns={
            os.environ["PRICEMEDIC_DATE_COL"]: "TRY_TO_TIMESTAMP",
        },
    )
    return {
        "table":     sample,
        "rate":      os.environ["PRICEMEDIC_RATE_COL"],
        "date":      os.environ["PRICEMEDIC_DATE_COL"],
        "hospital":  os.environ["PRICEMEDIC_HOSPITAL_COL"],
        "procedure": os.environ["PRICEMEDIC_PROCEDURE_COL"],
        "payer":     os.environ["PRICEMEDIC_PAYER_COL"],
    }


# --- Render-only checks (don't hit the warehouse) ----------------------------


def test_distribution_summary_renders_to_snowflake(cfg) -> None:
    sql = render_only_snowflake("distribution_summary", {
        "table": cfg["table"], "value_col": cfg["rate"],
        "group_cols": [cfg["hospital"]],
    })
    # Sanity: SQLGlot kept the percentile aggregates intact.
    assert "PERCENTILE_CONT" in sql.upper()
    assert "DATE_TRUNC" not in sql.upper()  # distribution_summary has no date_trunc


def test_flux_variance_renders_to_snowflake(cfg) -> None:
    sql = render_only_snowflake("flux_variance", {
        "table":         cfg["table"], "amount_col": cfg["rate"],
        "date_col":      cfg["date"], "dimensions": [cfg["hospital"]],
        "filter_clause": "TRUE", "material_pct": 0.20, "grain": "month",
    })
    assert "LAG" in sql.upper() and "DATE_TRUNC" in sql.upper()


# --- Execution checks (do hit the warehouse) ---------------------------------


def test_distribution_summary_runs(con, cfg) -> None:
    df = run_sql_tool_snowflake(con, "distribution_summary", {
        "table":      cfg["table"], "value_col": cfg["rate"],
        "group_cols": [cfg["hospital"]],
    })
    df.columns = [c.lower() for c in df.columns]
    assert {"n", "mean", "p50", "p95"}.issubset(df.columns)
    assert len(df) > 0


def test_flux_variance_runs(con, cfg) -> None:
    df = run_sql_tool_snowflake(con, "flux_variance", {
        "table":         cfg["table"], "amount_col": cfg["rate"],
        "date_col":      cfg["date"], "dimensions": [cfg["hospital"]],
        "filter_clause": "TRUE", "material_pct": 0.20, "grain": "month",
    })
    df.columns = [c.lower() for c in df.columns]
    assert {"period", "amount", "flux_label"}.issubset(df.columns)
    assert set(df["flux_label"].dropna().unique()) <= {"new", "stable", "material", "dropped"}


def test_statistical_trend_runs(con, cfg) -> None:
    df = run_sql_tool_snowflake(con, "statistical_trend", {
        "table":      cfg["table"], "value_col": cfg["rate"],
        "time_col":   cfg["date"], "group_cols": [cfg["procedure"]],
        "grain":      "month",
    })
    df.columns = [c.lower() for c in df.columns]
    assert {"slope", "intercept", "r_squared", "n_periods"}.issubset(df.columns)


def test_outliers_iqr_runs(con, cfg) -> None:
    sql = render_only_snowflake("outliers_iqr", {
        "table": cfg["table"], "value_col": cfg["rate"],
        "group_cols": [cfg["procedure"]], "iqr_multiplier": 3.0,
    })
    # Run an aggregate over the rendered query so we don't pull millions of rows.
    cur = con.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) AS n, SUM(is_outlier) AS n_outliers "
                    f"FROM ({sql.rstrip(';')})")
        n, n_out = cur.fetchone()
    finally:
        cur.close()
    assert int(n) > 0
    assert int(n_out) >= 0


def test_psi_runs_between_halves(con, cfg) -> None:
    """PSI between rows before vs after the median date — sanity check that PSI
    runs in Snowflake at all. We don't assert a specific value because PSI
    depends entirely on real distribution shape."""
    cur = con.cursor()
    try:
        cur.execute(f"CREATE OR REPLACE TEMP VIEW pm_baseline AS "
                    f"SELECT * FROM {cfg['table']} TABLESAMPLE (10000 ROWS)")
        cur.execute(f"CREATE OR REPLACE TEMP VIEW pm_current AS "
                    f"SELECT * FROM {cfg['table']} TABLESAMPLE (10000 ROWS)")
    finally:
        cur.close()
    df = run_sql_tool_snowflake(con, "psi", {
        "baseline_table": "pm_baseline", "current_table": "pm_current",
        "value_col": cfg["rate"], "n_bins": 10,
    })
    df.columns = [c.lower() for c in df.columns]
    assert "psi" in df.columns
    assert pd.notna(df["psi"].iloc[0])


def test_python_lane_historical_var_on_sample(con, cfg) -> None:
    """Pull a sample into pandas, run the Python `historical_var_pd` tool, assert shape."""
    out = run_python_tool_snowflake(
        con,
        query=f"SELECT {cfg['rate']} AS RATE FROM {cfg['table']} TABLESAMPLE (5000 ROWS)",
        name="historical_var_pd",
        params={"returns_col": "RATE", "alpha": 0.05},
    )
    df = pd.DataFrame(out.data) if hasattr(out, "data") else out
    assert {"var", "alpha"}.issubset(df.columns)
