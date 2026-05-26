"""Cross-lane parity: a SQL template and its `_pd` Python sibling must agree on
numerical results for the same input. Pins six representative tools across
descriptive / windows / anomaly / operations / trend / risk categories.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from nexcraft_jobs.compute.dstools_runner import run_python_tool, run_sql_tool


# --- Fixtures ----------------------------------------------------------------


def _facts() -> pa.Table:
    rng = np.random.default_rng(42)
    n_per_proj = 30
    projects = ["P-100", "P-200", "P-300"]
    rows = []
    start = datetime(2026, 1, 1)
    for proj in projects:
        for i in range(n_per_proj):
            rows.append((proj, start + timedelta(days=i), float(rng.normal(100, 12))))
    # Inject one outlier so anomaly detection has something to find. Use a
    # unique day so (project, ts) is unique — otherwise tie-breaking between
    # SQL and pandas can swap row order and break moving-window parity.
    rows.append(("P-100", start + timedelta(days=n_per_proj + 1), 9_999.0))
    return pa.table({
        "project": pa.array([r[0] for r in rows]),
        "ts":      pa.array([r[1] for r in rows]),
        "amount":  pa.array([r[2] for r in rows]),
    })


@pytest.fixture
def con():
    c = duckdb.connect(database=":memory:")
    c.register("facts", _facts())
    yield c
    c.close()


@pytest.fixture
def facts_df(con):
    return con.execute("SELECT * FROM facts").df()


def _pd_table(out):
    return pa.Table.from_pylist(out.data).to_pandas()


# --- Parity checks -----------------------------------------------------------


def test_parity_mean_grouped(con, facts_df) -> None:
    params = {"table": "facts", "value_col": "amount", "group_cols": ["project"]}
    sql = run_sql_tool(con, "mean", params).to_pandas().sort_values("project").reset_index(drop=True)
    py  = _pd_table(run_python_tool("mean_pd", {"df": facts_df, "value_col": "amount",
                                                 "group_cols": ["project"]})).sort_values("project").reset_index(drop=True)
    np.testing.assert_allclose(sql["mean"].to_numpy(), py["mean"].to_numpy(), rtol=1e-9)
    assert list(sql["n"]) == list(py["n"])


def test_parity_moving_average(con, facts_df) -> None:
    params = {"table": "facts", "series_col": "amount", "time_col": "ts",
              "window": 5, "partition_cols": ["project"]}
    sql = run_sql_tool(con, "moving_average", params).to_pandas()
    py  = _pd_table(run_python_tool("moving_average_pd",
        {"df": facts_df, "series_col": "amount", "time_col": "ts",
         "window": 5, "partition_cols": ["project"]}))
    key = ["project", "ts"]
    a = sql.sort_values(key).reset_index(drop=True)
    b = py.sort_values(key).reset_index(drop=True)
    np.testing.assert_allclose(a["moving_average"].to_numpy(),
                               b["moving_average"].to_numpy(),
                               rtol=1e-9, atol=1e-9)


def test_parity_outliers_iqr_flags(con, facts_df) -> None:
    params = {"table": "facts", "value_col": "amount", "group_cols": ["project"],
              "iqr_multiplier": 1.5}
    sql = run_sql_tool(con, "outliers_iqr", params).to_pandas()
    py  = _pd_table(run_python_tool("outliers_iqr_pd",
        {"df": facts_df, "value_col": "amount", "group_cols": ["project"],
         "iqr_multiplier": 1.5}))
    key = ["project", "ts", "amount"]
    a = sql.sort_values(key).reset_index(drop=True)
    b = py.sort_values(key).reset_index(drop=True)
    assert list(a["is_outlier"]) == list(b["is_outlier"])


def test_parity_flux_variance_labels(con, facts_df) -> None:
    params = {"table": "facts", "amount_col": "amount", "date_col": "ts",
              "dimensions": ["project"], "filter_clause": "TRUE",
              "material_pct": 0.20, "grain": "month"}
    sql = run_sql_tool(con, "flux_variance", params).to_pandas()
    py  = _pd_table(run_python_tool("flux_variance_pd",
        {"df": facts_df, "amount_col": "amount", "date_col": "ts",
         "dimensions": ["project"], "grain": "month", "material_pct": 0.20}))
    key = ["project", "period"]
    a = sql.sort_values(key).reset_index(drop=True)
    b = py.sort_values(key).reset_index(drop=True)
    # Labels and amounts should match. Flux/flux_pct may be NaN where no prior.
    assert list(a["flux_label"]) == list(b["flux_label"])
    np.testing.assert_allclose(a["amount"].to_numpy(), b["amount"].to_numpy(), rtol=1e-9)


def test_parity_statistical_trend_slope(con, facts_df) -> None:
    params = {"table": "facts", "value_col": "amount", "time_col": "ts",
              "group_cols": ["project"], "grain": "day"}
    sql = run_sql_tool(con, "statistical_trend", params).to_pandas().sort_values("project").reset_index(drop=True)
    py  = _pd_table(run_python_tool("statistical_trend_pd",
        {"df": facts_df, "value_col": "amount", "time_col": "ts",
         "group_cols": ["project"], "grain": "day"})).sort_values("project").reset_index(drop=True)
    np.testing.assert_allclose(sql["slope"].astype(float).to_numpy(),
                               py["slope"].astype(float).to_numpy(),
                               rtol=1e-6, atol=1e-6)


def test_parity_historical_var(con, facts_df) -> None:
    # Use a returns-like column (z-scored amount) so VaR is well-defined.
    facts_df = facts_df.copy()
    facts_df["returns"] = (facts_df["amount"] - facts_df["amount"].mean()) / facts_df["amount"].std()
    con.register("facts2", pa.Table.from_pandas(facts_df))
    sql = run_sql_tool(con, "historical_var",
                       {"table": "facts2", "returns_col": "returns", "alpha": 0.05}).to_pandas()
    py  = _pd_table(run_python_tool("historical_var_pd",
        {"df": facts_df, "returns_col": "returns", "alpha": 0.05}))
    np.testing.assert_allclose(float(sql["var"].iloc[0]), float(py["var"].iloc[0]), rtol=1e-9)
