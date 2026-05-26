"""Smoke test for the dstools runner.

Asserts that both lanes in the registry (SQL template and pure Python) produce
identical retention numbers for the same input data. This is the contract the
planner relies on when it picks between lanes for any given step.
"""
from __future__ import annotations

from datetime import datetime

import duckdb
import pyarrow as pa
import pytest

from dstools.contracts.errors import UnsafeIdentifierError
from dstools.sql.engine import render
from nexcraft_jobs.compute.dstools_runner import (
    render_only,
    run_python_tool,
    run_sql_tool,
)


def _events() -> pa.Table:
    rows = [
        ("u1", datetime(2026, 1, 5)),
        ("u1", datetime(2026, 2, 10)),
        ("u1", datetime(2026, 3, 1)),
        ("u2", datetime(2026, 1, 20)),
        ("u2", datetime(2026, 2, 15)),
        ("u3", datetime(2026, 2, 3)),
        ("u3", datetime(2026, 3, 4)),
        ("u4", datetime(2026, 3, 11)),
    ]
    return pa.table(
        {
            "user_id": pa.array([r[0] for r in rows]),
            "ts":      pa.array([r[1] for r in rows]),
        }
    )


@pytest.fixture
def con():
    c = duckdb.connect(database=":memory:")
    c.register("events", _events())
    yield c
    c.close()


def _sql_params() -> dict:
    return {
        "events_table":   "events",
        "user_col":       "user_id",
        "event_time_col": "ts",
        "event_filter":   "TRUE",
        "cohort_grain":   "month",
        "period_grain":   "month",
    }


def test_render_only_returns_duckdb_sql() -> None:
    sql = render_only("cohort_retention", _sql_params())
    assert "DATE_TRUNC('month'" in sql
    assert "NULLIF(size.cohort_size, 0)" in sql


def test_sql_lane_returns_expected_retention(con) -> None:
    result = run_sql_tool(con, "cohort_retention", _sql_params()).to_pandas()
    assert len(result) == 6
    # Jan cohort: u1+u2, retention 1.0, 1.0, 0.5 across Jan/Feb/Mar.
    jan = result[result["cohort"] == datetime(2026, 1, 1)].sort_values("period")
    assert list(jan["active"]) == [2, 2, 1]
    assert list(jan["cohort_size"]) == [2, 2, 2]
    assert list(jan["retention"]) == [1.0, 1.0, 0.5]


def test_sql_and_python_lanes_agree(con) -> None:
    sql_df = run_sql_tool(con, "cohort_retention", _sql_params()).to_pandas()
    df_events = con.execute("SELECT * FROM events").df()
    py_out = run_python_tool(
        "cohort_retention_pd",
        {
            "df":             df_events,
            "user_col":       "user_id",
            "event_time_col": "ts",
            "cohort_grain":   "month",
            "period_grain":   "month",
        },
    )
    py_df = pa.Table.from_pylist(py_out.data).to_pandas()

    key_cols = ["cohort", "period"]
    left = sql_df.sort_values(key_cols).reset_index(drop=True)
    right = py_df.sort_values(key_cols).reset_index(drop=True)

    assert list(left["active"]) == list(right["active"])
    assert list(left["cohort_size"]) == list(right["cohort_size"])
    assert list(left["retention"]) == list(right["retention"])


# --- Identifier-whitelist regression tests -----------------------------------


@pytest.mark.parametrize("ident", [
    "events",
    "schema.events",
    "MY_DB.RAW.events",
    "PRICEMEDIC_CORE_HOSPITAL__HEALTH_SYSTEM_RATES.SNAPSHOT_FEB_2026.V_PROVIDER_RATES",
    "_underscore_start",
    '"loID"',                                                  # Postgres/Snowflake quoted camelCase
    'csod_learn_datamodel."Transcript_csod"',                  # mixed quoted + bare
    'cornerstone_learn.csod_learn_datamodel."Activity_csod"',  # 3-part with quoted last
])
def test_identifier_whitelist_accepts_valid_names(ident: str) -> None:
    """Up to 3 dotted parts is the valid range — covers DB.SCHEMA.TABLE on
    Snowflake/BigQuery, and quoted "camelCase" forms on Postgres/Snowflake."""
    sql = render("descriptive.mean", {"table": ident, "value_col": "x"})
    assert ident in sql


@pytest.mark.parametrize("ident", [
    "events; DROP TABLE users",          # statement injection
    "events--comment",                   # comment injection
    "1nope",                             # leading digit
    "a.b.c.d",                           # four parts — too many
    "events name",                       # whitespace
    "events.col-with-dash",              # dash not allowed
    '"bad"name"',                        # embedded double quote
    '"',                                 # bare quote
    '"a.b"',                             # dot inside quotes
])
def test_identifier_whitelist_rejects_unsafe_names(ident: str) -> None:
    with pytest.raises(UnsafeIdentifierError):
        render("descriptive.mean", {"table": ident, "value_col": "x"})
