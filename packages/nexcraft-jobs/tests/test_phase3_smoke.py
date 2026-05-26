"""Phase 3 smoke tests: each module's canonical entry point runs without error
and returns the expected shape. These are guard-rail tests — they don't pin
exact numerical values, just shape/columns/nonzero output.
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

# statsmodels emits a noisy frequency warning when the index has a known freq
# but isn't explicitly tagged. Silence for this test module.
warnings.filterwarnings("ignore", category=Warning, module="statsmodels.*")


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture
def ts_df() -> pd.DataFrame:
    rng = np.random.default_rng(0)
    n = 60
    return pd.DataFrame({
        "ts":     pd.date_range("2026-01-01", periods=n, freq="D"),
        "y":      rng.normal(100, 10, n).cumsum(),
        "price":  rng.uniform(10, 20, n),
        "demand": rng.uniform(50, 100, n),
    })


@pytest.fixture
def did_df() -> pd.DataFrame:
    rng = np.random.default_rng(1)
    n = 200
    post  = np.repeat([0, 1], n // 2)
    treat = np.tile([0, 1], n // 2)
    y = 10 + 2 * post + 3 * treat + 4 * (post * treat) + rng.normal(0, 1, n)
    return pd.DataFrame({"y": y, "t": treat, "p": post})


@pytest.fixture
def survival_df() -> pd.DataFrame:
    rng = np.random.default_rng(2)
    return pd.DataFrame({
        "duration": rng.exponential(10, 100),
        "event":    rng.integers(0, 2, 100),
        "feature1": rng.normal(0, 1, 100),
    })


# --- Forecasting -------------------------------------------------------------


def test_forecasting_baselines_arima_ets(ts_df) -> None:
    from dstools.py.forecasting import (arima, drift_baseline, ets, forecast_metrics,
                                         naive_baseline)
    n_h = 5
    fc = naive_baseline(ts_df, value_col="y", time_col="ts", horizon=n_h)
    assert len(fc) == n_h and (fc["yhat"] == ts_df["y"].iloc[-1]).all()

    fc = drift_baseline(ts_df, value_col="y", time_col="ts", horizon=n_h)
    assert len(fc) == n_h and fc["yhat"].iloc[-1] > fc["yhat"].iloc[0]

    fc = arima(ts_df, value_col="y", time_col="ts", horizon=n_h, order=(1, 1, 1))
    assert len(fc) == n_h and {"yhat", "yhat_lower", "yhat_upper"}.issubset(fc.columns)

    fc = ets(ts_df, value_col="y", time_col="ts", horizon=n_h, trend="add", seasonal=None)
    assert len(fc) == n_h

    # forecast_metrics: just ensure all metrics are finite real numbers.
    m = forecast_metrics(pd.DataFrame({"y": ts_df["y"], "yhat": ts_df["y"] * 0.99}),
                         actual_col="y", predicted_col="yhat")
    assert set(["mape", "smape", "wape", "mae", "rmse", "mase"]).issubset(m.columns)


# --- Causal ------------------------------------------------------------------


def test_did_recovers_known_effect(did_df) -> None:
    from dstools.py.causal import did
    out = did(did_df, outcome_col="y", treatment_col="t", post_col="p")
    est = float(out["did_estimate"].iloc[0])
    # True DiD coefficient is 4 by construction.
    assert 3.0 < est < 5.0
    assert float(out["p_value"].iloc[0]) < 0.01


# --- Pricing -----------------------------------------------------------------


def test_price_elasticity_runs(ts_df) -> None:
    from dstools.py.pricing import price_elasticity
    out = price_elasticity(ts_df, demand_col="demand", price_col="price")
    assert {"elasticity", "intercept", "r_squared", "n"}.issubset(out.columns)


# --- Segmentation ML ---------------------------------------------------------


def test_segmentation_kmeans_returns_k_clusters(ts_df) -> None:
    from dstools.py.segmentation import run_kmeans
    out = run_kmeans(ts_df, feature_cols=["y", "price", "demand"], k=3, random_state=0)
    assert out["segment"].nunique() == 3
    assert "cluster_center_distance" in out.columns


# --- Survival ----------------------------------------------------------------


def test_kaplan_meier_monotone_nonincreasing(survival_df) -> None:
    from dstools.py.survival import kaplan_meier
    km = kaplan_meier(survival_df, duration_col="duration", event_col="event")
    s = km["survival"].dropna().to_numpy()
    # KM survival should be monotone non-increasing.
    assert all(s[i + 1] <= s[i] + 1e-9 for i in range(len(s) - 1))


def test_log_rank_returns_pvalue(survival_df) -> None:
    from dstools.py.survival import log_rank
    survival_df = survival_df.assign(arm=(survival_df["feature1"] > 0).astype(int))
    out = log_rank(survival_df, duration_col="duration", event_col="event", group_col="arm")
    assert 0.0 <= float(out["p_value"].iloc[0]) <= 1.0


# --- Drift -------------------------------------------------------------------


def test_drift_detects_shift(ts_df) -> None:
    from dstools.py.drift import ks_test, jensen_shannon, wasserstein
    a = ts_df.iloc[:30]
    b = ts_df.iloc[30:]
    # `y` is a random walk -> halves are strongly different.
    assert float(ks_test(a, b, col="y")["p_value"].iloc[0]) < 0.01
    assert float(jensen_shannon(a, b, col="y")["jensen_shannon"].iloc[0]) > 0.05
    assert float(wasserstein(a, b, col="y")["wasserstein"].iloc[0]) > 0


# --- Drift SQL ---------------------------------------------------------------


def test_psi_runs_on_warehouse(ts_df) -> None:
    import duckdb
    from nexcraft_jobs.compute.dstools_runner import run_sql_tool
    a = ts_df.iloc[:30]
    b = ts_df.iloc[30:]
    con = duckdb.connect()
    con.register("baseline", a)
    con.register("current",  b)
    out = run_sql_tool(con, "psi", {
        "baseline_table": "baseline", "current_table": "current",
        "value_col": "y", "n_bins": 10,
    }).to_pandas()
    assert "psi" in out.columns
    assert pd.notna(out["psi"].iloc[0])
