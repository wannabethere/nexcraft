"""Smoke tests for the final wave: experiments, attribution advanced, anomaly
advanced, risk advanced, ts_diag, and SPC SQL templates. Shape and "does it run"
checks only — these are guards, not algorithmic regressions."""
from __future__ import annotations

import warnings

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from nexcraft_jobs.compute.dstools_runner import run_python_tool, run_sql_tool

warnings.filterwarnings("ignore", category=Warning, module="statsmodels.*")


# --- Fixtures ----------------------------------------------------------------

@pytest.fixture
def ab_df() -> pd.DataFrame:
    rng = np.random.default_rng(0)
    n = 400
    arm = np.array(["control"] * (n // 2) + ["treatment"] * (n // 2))
    rate = np.where(arm == "treatment", 0.20, 0.10)
    return pd.DataFrame({
        "arm": arm,
        "converted": rng.binomial(1, rate),
        "value":     rng.normal(50, 5, n) + np.where(arm == "treatment", 1.5, 0),
        "cov":       rng.normal(0, 1, n),
    })


@pytest.fixture
def events_df() -> pd.DataFrame:
    rng = np.random.default_rng(1)
    rows = []
    for u in range(200):
        path = list(rng.choice(["email", "search", "display"], size=rng.integers(1, 4), replace=True))
        converted = rng.random() < (0.4 if "email" in path else 0.15)
        t = pd.Timestamp("2026-01-01")
        for c in path:
            rows.append((u, "view", t, c))
            t += pd.Timedelta(days=1)
        if converted:
            rows.append((u, "purchase", t, "n/a"))
    return pd.DataFrame(rows, columns=["user_id", "event", "ts", "channel"])


@pytest.fixture
def ts_df() -> pd.DataFrame:
    rng = np.random.default_rng(2)
    n = 200
    return pd.DataFrame({
        "ts": pd.date_range("2026-01-01", periods=n, freq="D"),
        "y":  np.sin(np.arange(n) / 10.0) * 5 + rng.normal(0, 0.5, n) + np.arange(n) * 0.05,
        "x":  rng.normal(0, 1, n),
    })


@pytest.fixture
def returns_df() -> pd.DataFrame:
    rng = np.random.default_rng(3)
    return pd.DataFrame({
        "ts":  pd.date_range("2026-01-01", periods=250, freq="D"),
        "asset": np.tile(["A", "B"], 125),
        "returns": rng.normal(0, 0.01, 250),
    })


# --- Experiments -------------------------------------------------------------

def test_cuped_v2_reduces_variance(ab_df) -> None:
    out = run_python_tool("cuped_v2", {"df": ab_df, "value_col": "value",
                                        "covariate_cols": ["cov"]})
    row = pa.Table.from_pylist(out.data).to_pandas().iloc[0]
    assert row["y_var_cuped"] <= row["y_var_raw"] + 1e-9


def test_srm_detected_on_imbalanced_split() -> None:
    df = pd.DataFrame({"arm": ["A"] * 600 + ["B"] * 400})
    out = run_python_tool("srm_check", {"df": df, "arm_col": "arm"})
    row = pa.Table.from_pylist(out.data).to_pandas().iloc[0]
    assert int(row["srm_detected"]) == 1


def test_bayesian_ab_returns_lift_ci(ab_df) -> None:
    out = run_python_tool("bayesian_ab", {"df": ab_df, "arm_col": "arm",
                                           "event_col": "converted",
                                           "control": "control", "treatment": "treatment",
                                           "n_samples": 5000})
    row = pa.Table.from_pylist(out.data).to_pandas().iloc[0]
    # Treatment rate (~0.20) >> control (~0.10) → P(treatment better) close to 1.
    assert row["p_treatment_better"] > 0.9


def test_aa_simulation_false_positive_near_alpha(ab_df) -> None:
    out = run_python_tool("aa_simulation", {"df": ab_df, "value_col": "value",
                                             "n_runs": 200, "alpha": 0.05})
    row = pa.Table.from_pylist(out.data).to_pandas().iloc[0]
    # Across 200 random splits, FPR should land roughly near 0.05.
    assert 0.0 <= float(row["false_positive_rate"]) <= 0.15


# --- Attribution -------------------------------------------------------------

def test_markov_attribution_emits_per_channel_credit(events_df) -> None:
    out = run_python_tool("markov_attribution", {"df": events_df, "user_col": "user_id",
                                                   "event_col": "event", "event_time_col": "ts",
                                                   "channel_col": "channel",
                                                   "conversion_event": "purchase"})
    res = pa.Table.from_pylist(out.data).to_pandas()
    assert set(res.columns) >= {"channel", "removal_effect", "attributed_conversions"}
    assert (res["attributed_conversions"] >= 0).all()


# --- Anomaly advanced -------------------------------------------------------

def test_seasonal_residuals_returns_decomposition(ts_df) -> None:
    out = run_python_tool("seasonal_residuals", {"df": ts_df, "value_col": "y",
                                                  "time_col": "ts", "period": 7})
    res = pa.Table.from_pylist(out.data).to_pandas()
    assert {"trend", "seasonal", "residual"}.issubset(res.columns)


def test_detect_change_points_finds_break() -> None:
    rng = np.random.default_rng(0)
    y = np.concatenate([rng.normal(0, 1, 80), rng.normal(5, 1, 80)])
    df = pd.DataFrame({"ts": pd.date_range("2026-01-01", periods=160, freq="D"), "y": y})
    out = run_python_tool("detect_change_points", {"df": df, "value_col": "y",
                                                    "time_col": "ts", "penalty": 5.0})
    res = pa.Table.from_pylist(out.data).to_pandas()
    assert len(res) >= 1  # at least the obvious break around index 80


def test_forecast_and_detect_anomalies_shape(ts_df) -> None:
    out = run_python_tool("forecast_and_detect_anomalies",
                          {"df": ts_df, "value_col": "y", "time_col": "ts",
                           "horizon_lookback": 30})
    res = pa.Table.from_pylist(out.data).to_pandas()
    assert {"yhat", "residual", "z_score", "is_anomaly"}.issubset(res.columns)
    assert len(res) == 30


# --- Risk advanced -----------------------------------------------------------

def test_monte_carlo_terminal_distribution() -> None:
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"r": rng.normal(0.001, 0.01, 500)})
    out = run_python_tool("monte_carlo", {"df": df, "returns_col": "r",
                                           "n_simulations": 1000, "horizon": 10})
    row = pa.Table.from_pylist(out.data).to_pandas().iloc[0]
    assert row["p05"] < row["median_terminal"] < row["p95"]


def test_fit_distribution_returns_best(ab_df) -> None:
    out = run_python_tool("fit_distribution", {"df": ab_df, "value_col": "value"})
    res = pa.Table.from_pylist(out.data).to_pandas()
    assert "distribution" in res.columns and len(res) >= 1


def test_portfolio_risk_and_attribution() -> None:
    rng = np.random.default_rng(0)
    wide = pd.DataFrame({
        "A": rng.normal(0.001, 0.01, 200),
        "B": rng.normal(0.001, 0.02, 200),
    })
    pr = run_python_tool("portfolio_risk", {"df": wide,
                                              "weights": {"A": 0.6, "B": 0.4}, "alpha": 0.05})
    pr_row = pa.Table.from_pylist(pr.data).to_pandas().iloc[0]
    assert pr_row["portfolio_vol"] > 0
    ra = run_python_tool("risk_attribution", {"df": wide,
                                                "weights": {"A": 0.6, "B": 0.4}})
    ra_df = pa.Table.from_pylist(ra.data).to_pandas()
    # Contributions sum to ~1.
    assert abs(float(ra_df["contribution_to_variance"].sum()) - 1.0) < 1e-6


# --- TS Diagnostics ----------------------------------------------------------

def test_adf_kpss_complement_on_stationary_series() -> None:
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"y": rng.normal(0, 1, 200)})
    adf = run_python_tool("adf_test", {"df": df, "value_col": "y"})
    kpss = run_python_tool("kpss_test", {"df": df, "value_col": "y"})
    adf_row = pa.Table.from_pylist(adf.data).to_pandas().iloc[0]
    kpss_row = pa.Table.from_pylist(kpss.data).to_pandas().iloc[0]
    # On white noise: ADF rejects unit root, KPSS fails to reject stationarity.
    assert int(adf_row["stationary_at_5pct"]) == 1
    assert int(kpss_row["stationary_at_5pct"]) == 1


def test_acf_pacf_return_nlags_plus_one() -> None:
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"y": rng.normal(0, 1, 200)})
    acf  = pa.Table.from_pylist(run_python_tool("acf",  {"df": df, "value_col": "y", "nlags": 10}).data).to_pandas()
    pacf = pa.Table.from_pylist(run_python_tool("pacf", {"df": df, "value_col": "y", "nlags": 10}).data).to_pandas()
    assert len(acf) == 11 and len(pacf) == 11


# --- SPC SQL -----------------------------------------------------------------

def test_x_bar_r_chart_runs() -> None:
    rng = np.random.default_rng(0)
    df = pd.DataFrame({
        "value":    rng.normal(100, 5, 100),
        "subgroup": np.repeat(np.arange(20), 5),
    })
    con = duckdb.connect()
    con.register("readings", df)
    out = run_sql_tool(con, "x_bar_r_chart", {
        "table": "readings", "value_col": "value",
        "subgroup_col": "subgroup", "subgroup_size": 5,
    }).to_pandas()
    assert {"xbar", "r", "xbar_lcl", "xbar_ucl", "out_of_control"}.issubset(out.columns)
    assert len(out) == 20


def test_cp_cpk_returns_indices() -> None:
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"value": rng.normal(100, 2, 100)})
    con = duckdb.connect()
    con.register("readings", df)
    out = run_sql_tool(con, "cp_cpk", {
        "table": "readings", "value_col": "value",
        "lsl": 92, "usl": 108,
    }).to_pandas()
    row = out.iloc[0]
    assert row["cp"] > 0 and row["cpk"] > 0
