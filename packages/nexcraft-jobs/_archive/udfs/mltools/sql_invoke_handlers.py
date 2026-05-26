from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any, Callable

import numpy as np
import pyarrow as pa

from nexcraft_jobs.compute.udfs._arrowutil import parse_ts_value
from nexcraft_jobs.compute.udfs.sql_moving_averages import (
    calculate_bollinger_bands_arrow,
    calculate_cumulative_operations_arrow,
    calculate_ema_json_arrow,
    calculate_expanding_window_arrow,
    calculate_moving_correlation_arrow,
    calculate_moving_minmax_arrow,
    calculate_moving_quantiles_arrow,
    calculate_moving_rank_arrow,
    calculate_moving_sum_arrow,
    calculate_moving_variance_arrow,
    calculate_sma_arrow,
    calculate_time_weighted_ma_arrow,
    calculate_wma_arrow,
)


def _j(p: dict[str, Any], key: str, default: Any = None) -> Any:
    return p.get(key, default)


def _data_json(p: dict[str, Any]) -> str:
    d = _j(p, "p_data")
    if isinstance(d, str):
        return d
    return json.dumps(d or [])


def _ser1(col: pa.Array) -> str:
    return json.dumps(col[0].as_py(), default=str)


def _moving_row(name: str, fn: Callable[..., pa.Array], p: dict[str, Any], extra: list[Any]) -> str:
    j = _data_json(p)
    arrays = [pa.array([j])] + [pa.array([x]) for x in extra]
    return _ser1(fn(*arrays))


def _records_value_key(rows: list[dict[str, Any]]) -> tuple[str, str]:
    if not rows:
        return "value", "time"
    if "value" in rows[0]:
        return "value", "time"
    return "metric", "time"


def _series_keys(rows: list[Any]) -> tuple[str, str]:
    if isinstance(rows, list) and rows:
        return _records_value_key(rows)
    return ("value", "time")


def _rows_sorted(rows: list[dict[str, Any]], vk: str, tk: str) -> list[tuple[datetime, float]]:
    pts: list[tuple[datetime, float]] = []
    for e in rows:
        if not isinstance(e, dict):
            continue
        ts = parse_ts_value(e, tk)
        if ts is None:
            continue
        try:
            v = float(e.get(vk))
        except (TypeError, ValueError):
            continue
        pts.append((ts, v))
    pts.sort(key=lambda x: x[0])
    return pts


def _lag_table(p: dict[str, Any]) -> str:
    rows = _j(p, "p_data") or []
    if not isinstance(rows, list):
        return json.dumps([])
    vk, tk = _records_value_key(rows)
    lag = int(_j(p, "p_lag_periods", 1))
    pts = _rows_sorted(rows, vk, tk)
    out: list[dict[str, Any]] = []
    for i, (ts, val) in enumerate(pts):
        lagged = pts[i - lag][1] if i >= lag else None
        out.append(
            {
                "row_number": i + 1,
                "time_period": ts.isoformat(),
                "original_value": val,
                "lagged_value": lagged,
                "absolute_change": (val - lagged) if lagged is not None else None,
                "percent_change": ((val - lagged) / lagged * 100) if lagged not in (None, 0) else None,
                "lag_periods": lag,
            }
        )
    return json.dumps(out)


def _lead_table(p: dict[str, Any]) -> str:
    rows = _j(p, "p_data") or []
    if not isinstance(rows, list):
        return json.dumps([])
    vk, tk = _records_value_key(rows)
    lead = int(_j(p, "p_lead_periods", 1))
    pts = _rows_sorted(rows, vk, tk)
    out: list[dict[str, Any]] = []
    for i, (ts, val) in enumerate(pts):
        lead_v = pts[i + lead][1] if i + lead < len(pts) else None
        out.append(
            {
                "row_number": i + 1,
                "time_period": ts.isoformat(),
                "original_value": val,
                "lead_value": lead_v,
                "absolute_change": (lead_v - val) if lead_v is not None else None,
                "percent_change": ((lead_v - val) / val * 100) if val not in (0, None) and lead_v is not None else None,
                "lead_periods": lead,
            }
        )
    return json.dumps(out)


def _aggregate_by_time(p: dict[str, Any]) -> str:
    rows = _j(p, "p_data") or []
    if not isinstance(rows, list):
        return json.dumps([])
    tcol = str(_j(p, "p_time_column", "time"))
    mcol = str(_j(p, "p_metric_column", "metric"))
    period = str(_j(p, "p_period", "day"))
    agg = str(_j(p, "p_aggregation", "sum")).lower()
    buckets: dict[str, list[float]] = {}
    for e in rows:
        if not isinstance(e, dict):
            continue
        ts = parse_ts_value(e, tcol)
        if ts is None:
            continue
        try:
            mv = float(e.get(mcol))
        except (TypeError, ValueError):
            continue
        if period == "hour":
            key = ts.replace(minute=0, second=0, microsecond=0)
        elif period == "day":
            key = ts.date().isoformat()
        elif period == "week":
            key = ts.isocalendar().week
            key = f"{ts.isocalendar().year}-W{key:02d}"
        elif period == "month":
            key = ts.strftime("%Y-%m")
        elif period == "quarter":
            q = (ts.month - 1) // 3 + 1
            key = f"{ts.year}-Q{q}"
        elif period == "year":
            key = str(ts.year)
        else:
            key = ts.date().isoformat()
        buckets.setdefault(str(key), []).append(mv)
    out: list[dict[str, Any]] = []
    for k in sorted(buckets.keys()):
        vals = np.array(buckets[k], dtype=np.float64)
        if agg == "sum":
            agg_v = float(np.sum(vals))
        elif agg == "avg":
            agg_v = float(np.mean(vals))
        elif agg == "min":
            agg_v = float(np.min(vals))
        elif agg == "max":
            agg_v = float(np.max(vals))
        elif agg == "count":
            agg_v = float(len(vals))
        elif agg == "stddev":
            agg_v = float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0
        else:
            agg_v = float(np.sum(vals))
        out.append(
            {
                "time_period": k,
                "aggregated_value": agg_v,
                "record_count": int(len(vals)),
                "min_value": float(np.min(vals)),
                "max_value": float(np.max(vals)),
                "avg_value": float(np.mean(vals)),
                "stddev_value": float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0,
            }
        )
    return json.dumps(out)


def _detect_anomalies(p: dict[str, Any]) -> str:
    rows = _j(p, "p_data") or []
    if not isinstance(rows, list):
        return json.dumps([])
    vk, tk = _records_value_key(rows)
    pts = _rows_sorted(rows, vk, tk)
    vals = np.array([v for _, v in pts], dtype=np.float64)
    thr = float(_j(p, "p_threshold_std", 2.0))
    method = str(_j(p, "p_method", "zscore")).lower()
    out: list[dict[str, Any]] = []
    if vals.size == 0:
        return json.dumps([])
    mean = float(np.mean(vals))
    std = float(np.std(vals, ddof=1)) if vals.size > 1 else 0.0
    for i, (ts, v) in enumerate(pts):
        if method == "iqr":
            q1, q3 = np.percentile(vals, [25, 75])
            iqr = q3 - q1
            lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
            is_a = v < lo or v > hi
            score = abs(v - mean) / (std + 1e-9)
        else:
            z = abs((v - mean) / std) if std > 0 else 0.0
            is_a = z > thr
            score = z
        out.append(
            {
                "time_period": ts.isoformat(),
                "metric_value": v,
                "is_anomaly": bool(is_a),
                "anomaly_score": float(score),
                "deviation_from_mean": float(v - mean),
                "z_score": float((v - mean) / std) if std > 0 else 0.0,
                "anomaly_type": "high" if v > mean else "low" if is_a else "none",
            }
        )
    return json.dumps(out)


def _statistical_trend(p: dict[str, Any]) -> str:
    rows = _j(p, "p_data") or []
    if not isinstance(rows, list):
        return json.dumps([])
    vk, tk = _records_value_key(rows)
    pts = _rows_sorted(rows, vk, tk)
    ys = np.array([v for _, v in pts], dtype=np.float64)
    if ys.size < 2:
        return json.dumps([])
    x = np.arange(ys.size, dtype=np.float64)
    slope, intercept = np.polyfit(x, ys, 1)
    y_hat = slope * x + intercept
    ss_res = float(np.sum((ys - y_hat) ** 2))
    ss_tot = float(np.sum((ys - ys.mean()) ** 2)) or 1.0
    r2 = 1.0 - ss_res / ss_tot
    direction = "increasing" if slope > 0 else "decreasing" if slope < 0 else "stable"
    return json.dumps(
        {
            "trend_direction": direction,
            "slope": float(slope),
            "intercept": float(intercept),
            "r_squared": float(r2),
            "correlation": float(np.corrcoef(x, ys)[0, 1]) if ys.std() > 0 else 0.0,
            "p_value": None,
            "is_significant": bool(r2 > 0.5),
            "data_points": int(ys.size),
            "trend_strength": "strong" if r2 > 0.7 else "moderate" if r2 > 0.4 else "weak",
        }
    )


def _percent_change_comparison(p: dict[str, Any]) -> str:
    rows = _j(p, "p_data") or []
    cond = str(_j(p, "p_condition_column", "condition"))
    base = str(_j(p, "p_baseline_value", "control"))
    if not isinstance(rows, list):
        return json.dumps([])
    from collections import defaultdict

    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for e in rows:
        if not isinstance(e, dict):
            continue
        c = str(e.get(cond, ""))
        m = str(e.get("metric", "value"))
        try:
            v = float(e.get("value", e.get("metric")))
        except (TypeError, ValueError):
            continue
        buckets[(c, m)].append(v)
    out: list[dict[str, Any]] = []
    for (c, m), vals in buckets.items():
        if c == base:
            continue
        bmean = float(np.mean(buckets.get((base, m), [0]) or [0]))
        tmean = float(np.mean(vals))
        out.append(
            {
                "condition_value": c,
                "metric_name": m,
                "baseline_avg": bmean,
                "treatment_avg": tmean,
                "absolute_change": tmean - bmean,
                "percent_change": ((tmean - bmean) / bmean * 100) if bmean else None,
                "relative_uplift": ((tmean / bmean) - 1.0) * 100 if bmean else None,
            }
        )
    return json.dumps(out)


def _bootstrap_ci(p: dict[str, Any]) -> str:
    rows = _j(p, "p_data") or []
    vals: list[float] = []
    if isinstance(rows, list):
        for e in rows:
            if isinstance(e, dict) and "value" in e:
                try:
                    vals.append(float(e["value"]))
                except (TypeError, ValueError):
                    pass
    a = np.array(vals, dtype=np.float64)
    if a.size == 0:
        return json.dumps([])
    metric = str(_j(p, "p_metric", "mean"))
    conf = float(_j(p, "p_confidence_level", 95))
    n_boot = int(_j(p, "p_bootstrap_samples", 500))
    rng = np.random.default_rng(42)
    stats: list[float] = []
    for _ in range(min(n_boot, 2000)):
        samp = rng.choice(a, size=a.size, replace=True)
        if metric == "median":
            stats.append(float(np.median(samp)))
        elif metric == "std":
            stats.append(float(np.std(samp, ddof=1)))
        else:
            stats.append(float(np.mean(samp)))
    lo = float(np.percentile(stats, (100 - conf) / 2))
    hi = float(np.percentile(stats, 100 - (100 - conf) / 2))
    return json.dumps(
        {
            "metric_type": metric,
            "point_estimate": float(np.mean(a)),
            "ci_lower": lo,
            "ci_upper": hi,
            "confidence_level": conf,
            "sample_size": int(a.size),
        }
    )


def _power_analysis(p: dict[str, Any]) -> str:
    d = float(_j(p, "p_effect_size", 0))
    sd = float(_j(p, "p_baseline_std", 1)) or 1.0
    alpha = float(_j(p, "p_alpha", 0.05))
    power = float(_j(p, "p_power", 0.8))
    z_a = 1.96 if abs(alpha - 0.05) < 1e-6 else 1.645
    z_b = 0.84 if abs(power - 0.8) < 1e-6 else 1.28
    n = 2 * ((sd * (z_a + z_b) / max(d, 1e-9)) ** 2)
    return json.dumps(
        {
            "effect_size": d,
            "std_deviation": sd,
            "alpha_level": alpha,
            "target_power": power,
            "required_sample_per_group": int(math.ceil(max(n, 2))),
            "total_sample_required": int(math.ceil(max(n, 2)) * 2),
            "cohens_d": d / sd if sd else None,
        }
    )


def _effect_sizes(p: dict[str, Any]) -> str:
    def _vals(x: Any) -> np.ndarray:
        if not isinstance(x, list):
            return np.array([], dtype=np.float64)
        out: list[float] = []
        for e in x:
            if isinstance(e, dict) and "value" in e:
                try:
                    out.append(float(e["value"]))
                except (TypeError, ValueError):
                    pass
        return np.array(out, dtype=np.float64)

    t = _vals(_j(p, "p_data_treatment"))
    c = _vals(_j(p, "p_data_control"))
    if t.size < 2 or c.size < 2:
        return json.dumps([])
    m1, m0 = float(np.mean(t)), float(np.mean(c))
    s1, s0 = float(np.std(t, ddof=1)), float(np.std(c, ddof=1))
    sp = math.sqrt(((t.size - 1) * s1**2 + (c.size - 1) * s0**2) / (t.size + c.size - 2))
    cohens_d = (m1 - m0) / sp if sp else 0.0
    return json.dumps(
        {
            "effect_size_type": "cohens_d",
            "effect_size_value": cohens_d,
            "interpretation": "small"
            if abs(cohens_d) < 0.5
            else "medium"
            if abs(cohens_d) < 0.8
            else "large",
            "treatment_mean": m1,
            "control_mean": m0,
            "pooled_std": sp,
        }
    )


def _bonferroni(p: dict[str, Any]) -> str:
    pvals = _j(p, "p_pvalues")
    alpha = float(_j(p, "p_alpha", 0.05))
    if not isinstance(pvals, list):
        return json.dumps([])
    m = len(pvals) or 1
    rows: list[dict[str, Any]] = []
    for i, pv in enumerate(pvals):
        try:
            fp = float(pv)
        except (TypeError, ValueError):
            continue
        adj = min(fp * m, 1.0)
        rows.append(
            {
                "comparison_number": i + 1,
                "original_pvalue": fp,
                "adjusted_pvalue": adj,
                "is_significant_original": fp < alpha,
                "is_significant_adjusted": adj < alpha,
                "bonferroni_correction": m,
            }
        )
    return json.dumps(rows)


def _find_correlated_metrics(p: dict[str, Any]) -> str:
    panel = _j(p, "panel") or _j(p, "p_panel") or []
    primary = str(_j(p, "p_primary_metric", _j(p, "primary_metric", "")))
    min_c = float(_j(p, "p_min_correlation", 0.6))
    if not isinstance(panel, list) or not primary:
        return json.dumps([])
    by_date: dict[str, dict[str, float]] = {}
    for r in panel:
        if not isinstance(r, dict):
            continue
        d = str(r.get("metric_date") or r.get("d") or r.get("date", ""))
        name = str(r.get("metric_name") or r.get("metric", ""))
        try:
            v = float(r.get("metric_value", r.get("val", r.get("value", 0))))
        except (TypeError, ValueError):
            continue
        by_date.setdefault(d, {})[name] = v
    dates = sorted(by_date.keys())
    others: dict[str, list[float]] = {}
    prim: list[float] = []
    for d in dates:
        row = by_date[d]
        if primary not in row:
            continue
        prim.append(row[primary])
        for k, v in row.items():
            if k == primary:
                continue
            others.setdefault(k, []).append(v)
    out: list[dict[str, Any]] = []
    for other, series in others.items():
        n = min(len(series), len(prim))
        if n < 3:
            continue
        a = np.array(prim[-n:], dtype=np.float64)
        b = np.array(series[-n:], dtype=np.float64)
        if np.std(a) == 0 or np.std(b) == 0:
            continue
        c = float(np.corrcoef(a, b)[0, 1])
        if abs(c) < min_c:
            continue
        out.append(
            {
                "metric_pair": f"{primary} ↔ {other}",
                "primary_metric": primary,
                "correlated_metric": other,
                "correlation": round(c, 4),
                "abs_correlation": round(abs(c), 4),
                "direction": "positive" if c >= 0 else "negative",
                "data_points": n,
                "window_start": dates[0] if dates else None,
                "window_end": dates[-1] if dates else None,
            }
        )
    out.sort(key=lambda r: r["abs_correlation"], reverse=True)
    return json.dumps(out)


def _calculate_lag_correlation(p: dict[str, Any]) -> str:
    return _find_correlated_metrics(p)


def _decompose_impact_by_dimension(p: dict[str, Any]) -> str:
    panel = _j(p, "panel") or []
    metric = str(_j(p, "p_metric_name", _j(p, "p_primary_metric", "")))
    dim = str(_j(p, "p_dimension", "region"))
    if not isinstance(panel, list):
        return json.dumps([])
    segments: dict[str, list[float]] = {}
    for r in panel:
        if not isinstance(r, dict):
            continue
        if str(r.get("metric_name", "")) != metric:
            continue
        seg = str(r.get(dim) or r.get("dimension_value", "unknown"))
        try:
            v = float(r.get("metric_value", r.get("value", 0)))
        except (TypeError, ValueError):
            continue
        segments.setdefault(seg, []).append(v)
    rows = [{"dimension_value": k, "anomaly_actual": float(np.mean(vs))} for k, vs in segments.items()]
    return json.dumps(rows)


def _build_anomaly_explanation_payload(p: dict[str, Any]) -> str:
    base = {
        "anomaly": {
            "metric": _j(p, "p_primary_metric"),
            "anomaly_date": str(_j(p, "p_anomaly_date", "")),
        },
        "correlations": json.loads(_find_correlated_metrics(p)) if _j(p, "panel") else [],
        "leading_indicators": [],
        "segment_breakdown": json.loads(_decompose_impact_by_dimension(p)) if _j(p, "panel") else [],
    }
    return json.dumps(base)


def _moving_average_trend(p: dict[str, Any]) -> str:
    j = _data_json(p)
    w = int(_j(p, "p_window_size", 7))
    ma = str(_j(p, "p_ma_type", "simple"))
    rows = json.loads(j)
    if ma != "simple":
        return json.dumps(
            {"note": "use calculate_wma/calculate_ema for weighted/exponential", "rows": []}
        )
    st = calculate_sma_arrow(pa.array([j]), pa.array([w]), pa.array([None]))
    return json.dumps(st[0].as_py(), default=str)


def _growth_rates(p: dict[str, Any]) -> str:
    rows = _j(p, "p_data") or []
    if not isinstance(rows, list):
        return json.dumps([])
    vk, tk = _records_value_key(rows) if rows else ("value", "time")
    pts = _rows_sorted(rows, vk, tk)
    out: list[dict[str, Any]] = []
    for i in range(1, len(pts)):
        prev, cur = pts[i - 1][1], pts[i][1]
        pct = ((cur - prev) / prev * 100) if prev else None
        out.append(
            {
                "time_period": pts[i][0].isoformat(),
                "current_value": cur,
                "previous_value": prev,
                "absolute_change": cur - prev,
                "percent_change": pct,
                "annualized_growth": None,
                "growth_category": "high" if pct and pct > 10 else "low",
            }
        )
    return json.dumps(out)


def _forecast_linear(p: dict[str, Any]) -> str:
    rows = _j(p, "p_data") or []
    ahead = int(_j(p, "p_periods_ahead", 7))
    vk, tk = _records_value_key(rows) if rows else ("value", "time")
    pts = _rows_sorted(rows, vk, tk)
    ys = np.array([v for _, v in pts], dtype=np.float64)
    if ys.size < 2:
        return json.dumps([])
    x = np.arange(ys.size, dtype=np.float64)
    slope, intercept = np.polyfit(x, ys, 1)
    out: list[dict[str, Any]] = []
    for h in range(1, ahead + 1):
        fv = slope * (ys.size - 1 + h) + intercept
        out.append({"forecast_period": h, "forecast_value": float(fv), "lower_bound": None, "upper_bound": None})
    return json.dumps(out)


def _volatility(p: dict[str, Any]) -> str:
    j = _data_json(p)
    w = int(_j(p, "p_window_size", 5))
    rows = json.loads(j)
    vk, tk = _records_value_key(rows) if rows else ("value", "time")
    pts = _rows_sorted(rows, vk, tk)
    vals = [v for _, v in pts]
    out: list[dict[str, Any]] = []
    for i in range(len(vals)):
        lo = max(0, i - w + 1)
        window = np.array(vals[lo : i + 1], dtype=np.float64)
        sd = float(np.std(window, ddof=1)) if window.size > 1 else 0.0
        out.append(
            {
                "time_period": pts[i][0].isoformat(),
                "metric_value": vals[i],
                "rolling_std": sd,
                "rolling_variance": sd**2,
                "coefficient_variation": sd / abs(np.mean(window)) * 100 if np.mean(window) else 0.0,
                "volatility_score": sd,
                "volatility_level": "moderate",
            }
        )
    return json.dumps(out)


def _compare_periods(p: dict[str, Any]) -> str:
    return _growth_rates(p)


def _detect_seasonality(p: dict[str, Any]) -> str:
    rows = _j(p, "p_data") or []
    vk, tk = _series_keys(rows)
    pts = _rows_sorted(rows, vk, tk)
    if not pts:
        return json.dumps([])
    months: dict[int, list[float]] = {}
    for ts, v in pts:
        months.setdefault(ts.month, []).append(v)
    out = [
        {
            "season_period": m,
            "average_value": float(np.mean(vs)),
            "std_dev": float(np.std(vs, ddof=1)) if len(vs) > 1 else 0.0,
            "min_value": float(np.min(vs)),
            "max_value": float(np.max(vs)),
            "coefficient_variation": 0.0,
            "seasonal_index": 1.0,
        }
        for m, vs in sorted(months.items())
    ]
    return json.dumps(out)


def _get_top_metrics(p: dict[str, Any]) -> str:
    raw = _j(p, "p_metrics_data") or {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        return json.dumps([])
    n = int(_j(p, "p_n", 5))
    crit = str(_j(p, "p_ranking_criteria", "growth"))
    scored: list[tuple[str, float]] = []
    for name, series in raw.items():
        if not isinstance(series, list) or len(series) < 2:
            continue
        ys = []
        for e in series:
            if isinstance(e, dict) and "value" in e:
                try:
                    ys.append(float(e["value"]))
                except (TypeError, ValueError):
                    pass
        if len(ys) < 2:
            continue
        g = (ys[-1] - ys[0]) / (abs(ys[0]) + 1e-9) * 100 if crit == "growth" else float(np.std(ys))
        scored.append((name, g))
    scored.sort(key=lambda x: abs(x[1]), reverse=True)
    out = [
        {
            "metric_name": name,
            "ranking_score": sc,
            "latest_value": None,
            "average_value": None,
            "growth_rate": sc,
            "volatility": 0.0,
            "rank_position": i + 1,
        }
        for i, (name, sc) in enumerate(scored[:n])
    ]
    return json.dumps(out)


def _cumulative_trend(p: dict[str, Any]) -> str:
    rows = _j(p, "p_data") or []
    ctype = str(_j(p, "p_cumulative_type", "sum"))
    vk, tk = _series_keys(rows)
    pts = _rows_sorted(rows, vk, tk)
    vals = [v for _, v in pts]
    arr = np.array(vals, dtype=np.float64)
    cum = np.cumsum(arr) if ctype == "sum" else np.maximum.accumulate(arr)
    tot = float(np.sum(vals)) or 1.0
    out = [
        {
            "time_period": pts[i][0].isoformat(),
            "period_value": vals[i],
            "cumulative_value": float(cum[i]),
            "cumulative_percent": float(cum[i] / tot * 100),
        }
        for i in range(len(vals))
    ]
    return json.dumps(out)


def _classify_trend(p: dict[str, Any]) -> str:
    st = json.loads(_statistical_trend(p))
    return json.dumps(
        {
            "overall_trend": st.get("trend_direction"),
            "trend_strength": st.get("trend_strength"),
            "direction_consistency": 1.0,
            "velocity": st.get("slope"),
            "acceleration": 0.0,
            "recommendation": "monitor",
        }
    )


def _analyze_variance(p: dict[str, Any]) -> str:
    return _moving_row(
        "analyze_variance",
        calculate_moving_variance_arrow,
        p,
        [int(_j(p, "p_window_size", 5)), str(_j(p, "p_group_by") or "")],
    )


def _analyze_distribution(p: dict[str, Any]) -> str:
    rows = _j(p, "p_data") or []
    if not isinstance(rows, list):
        return json.dumps([])
    vk, _ = _series_keys(rows)
    vals = []
    for e in rows:
        if not isinstance(e, dict):
            continue
        for k in (vk, "value", "metric"):
            if k in e:
                try:
                    vals.append(float(e[k]))
                except (TypeError, ValueError):
                    pass
                break
    a = np.array(vals, dtype=np.float64)
    if a.size == 0:
        return json.dumps([])
    return json.dumps(
        [
            {
                "group_name": "all",
                "count_values": int(a.size),
                "mean_value": float(np.mean(a)),
                "median_value": float(np.median(a)),
                "std_dev": float(np.std(a, ddof=1)) if a.size > 1 else 0.0,
                "skewness": float(((a - a.mean()) ** 3).mean() / (a.std() ** 3 + 1e-9)) if a.size > 2 else 0.0,
                "kurtosis": 0.0,
            }
        ]
    )


def _calculate_difference(p: dict[str, Any]) -> str:
    rows = _j(p, "p_data") or []
    order = int(_j(p, "p_order", 1))
    vk, tk = _series_keys(rows)
    pts = _rows_sorted(rows, vk, tk)
    vals = np.array([v for _, v in pts], dtype=np.float64)
    d1 = np.diff(vals, n=1, prepend=np.nan) if order >= 1 else vals
    d2 = np.diff(d1, n=1, prepend=np.nan) if order >= 2 else d1
    out = []
    for i, (ts, _) in enumerate(pts):
        out.append(
            {
                "row_number": i + 1,
                "time_period": ts.isoformat(),
                "original_value": float(vals[i]),
                "first_difference": float(d1[i]) if not math.isnan(d1[i]) else None,
                "second_difference": float(d2[i]) if order >= 2 and not math.isnan(d2[i]) else None,
                "is_stationary": False,
            }
        )
    return json.dumps(out)


def _calculate_cdf(p: dict[str, Any]) -> str:
    rows = _j(p, "p_data") or []
    vk, tk = _series_keys(rows)
    pts = _rows_sorted(rows, vk, tk)
    vals = np.sort(np.array([v for _, v in pts], dtype=np.float64))
    n = max(vals.size, 1)
    out = []
    for i, (ts, v) in enumerate(pts):
        rank = int(np.sum(vals <= v))
        out.append(
            {
                "row_number": i + 1,
                "time_period": ts.isoformat(),
                "original_value": v,
                "cdf_value": rank / n,
                "percentile_rank": rank / n * 100,
                "cumulative_count": rank,
                "group_name": "all",
            }
        )
    return json.dumps(out)


def _rolling_window(p: dict[str, Any]) -> str:
    j = _data_json(p)
    w = int(_j(p, "p_window_size", 5))
    agg = str(_j(p, "p_aggregation", "mean"))
    rows = json.loads(j)
    vk, tk = _series_keys(rows)
    pts = _rows_sorted(rows, vk, tk)
    vals = [v for _, v in pts]
    out = []
    for i in range(len(vals)):
        lo = max(0, i - w + 1)
        window = np.array(vals[lo : i + 1], dtype=np.float64)
        if agg == "sum":
            rv = float(np.sum(window))
        elif agg == "min":
            rv = float(np.min(window))
        elif agg == "max":
            rv = float(np.max(window))
        elif agg == "std":
            rv = float(np.std(window, ddof=1)) if window.size > 1 else 0.0
        elif agg == "count":
            rv = float(len(window))
        else:
            rv = float(np.mean(window))
        out.append(
            {
                "row_number": i + 1,
                "time_period": pts[i][0].isoformat(),
                "original_value": vals[i],
                "rolling_value": rv,
                "deviation_from_rolling": vals[i] - rv,
                "percent_deviation": ((vals[i] - rv) / rv * 100) if rv else None,
                "window_size": w,
            }
        )
    return json.dumps(out)


def _calculate_cumulative_ts(p: dict[str, Any]) -> str:
    rows = _j(p, "p_data") or []
    op = str(_j(p, "p_operation", "sum"))
    vk, tk = _series_keys(rows)
    pts = _rows_sorted(rows, vk, tk)
    vals = np.array([v for _, v in pts], dtype=np.float64)
    if op == "sum":
        c = np.cumsum(vals)
    elif op == "product":
        c = np.cumprod(vals)
    elif op == "max":
        c = np.maximum.accumulate(vals)
    elif op == "min":
        c = np.minimum.accumulate(vals)
    else:
        c = np.cumsum(vals)
    tot = float(np.sum(vals)) or 1.0
    out = [
        {
            "row_number": i + 1,
            "time_period": pts[i][0].isoformat(),
            "original_value": float(vals[i]),
            "cumulative_value": float(c[i]),
            "percent_of_total": float(c[i] / tot * 100),
        }
        for i in range(len(vals))
    ]
    return json.dumps(out)


def _calculate_percent_change(p: dict[str, Any]) -> str:
    rows = _j(p, "p_data") or []
    periods = int(_j(p, "p_periods", 1))
    method = str(_j(p, "p_method", "simple"))
    vk, tk = _series_keys(rows)
    pts = _rows_sorted(rows, vk, tk)
    vals = [v for _, v in pts]
    out = []
    for i in range(len(vals)):
        prev_i = i - periods
        prev = vals[prev_i] if prev_i >= 0 else None
        cur = vals[i]
        if prev is None:
            out.append({})
            continue
        if method == "log":
            ch = math.log(cur / prev) if prev > 0 and cur > 0 else None
            pct = None
        else:
            pct = (cur - prev) / prev * 100 if prev else None
            ch = cur - prev
        out.append(
            {
                "row_number": i + 1,
                "time_period": pts[i][0].isoformat(),
                "original_value": cur,
                "previous_value": prev,
                "absolute_change": ch if method != "log" else None,
                "percent_change": pct,
                "log_change": ch if method == "log" else None,
                "change_category": "large" if pct and abs(pct) > 10 else "small",
            }
        )
    return json.dumps(out)


def _autocorrelation(p: dict[str, Any]) -> str:
    rows = _j(p, "p_data") or []
    max_lag = int(_j(p, "p_max_lag", 10))
    vk, tk = _series_keys(rows)
    pts = _rows_sorted(rows, vk, tk)
    y = np.array([v for _, v in pts], dtype=np.float64)
    y = y - y.mean()
    out = []
    for lag in range(1, min(max_lag, len(y) - 1) + 1):
        if len(y) <= lag:
            break
        c = float(np.corrcoef(y[:-lag], y[lag:])[0, 1]) if np.std(y[:-lag]) and np.std(y[lag:]) else 0.0
        bound = 1.96 / math.sqrt(len(y))
        out.append(
            {
                "lag_period": lag,
                "autocorrelation": c,
                "is_significant": abs(c) > bound,
                "confidence_lower": -bound,
                "confidence_upper": bound,
            }
        )
    return json.dumps(out)


def _test_stationarity(p: dict[str, Any]) -> str:
    rows = _j(p, "p_data") or []
    vk, tk = _series_keys(rows)
    pts = _rows_sorted(rows, vk, tk)
    y = np.array([v for _, v in pts], dtype=np.float64)
    if y.size < 3:
        return json.dumps([])
    slope, _ = np.polyfit(np.arange(y.size, dtype=np.float64), y, 1)
    return json.dumps(
        [
            {
                "test_name": "quick_variance_trend",
                "is_stationary": abs(slope) < 1e-6,
                "mean_value": float(np.mean(y)),
                "variance": float(np.var(y, ddof=1)),
                "trend_slope": float(slope),
                "recommendation": "series shows trend" if abs(slope) >= 1e-6 else "roughly stable",
            }
        ]
    )


def _absolute_change_comparison(p: dict[str, Any]) -> str:
    return _percent_change_comparison(p)


def _prepost_comparison(p: dict[str, Any]) -> str:
    return json.dumps([])


def _stratified_analysis(p: dict[str, Any]) -> str:
    return json.dumps([])


def _sequential_analysis(p: dict[str, Any]) -> str:
    return json.dumps([])


def _cuped_adjustment(p: dict[str, Any]) -> str:
    return json.dumps([])


def build_handlers() -> dict[str, Callable[[dict[str, Any]], str]]:
    h: dict[str, Callable[[dict[str, Any]], str]] = {}

    def sma(p):
        return _moving_row(
            "calculate_sma",
            calculate_sma_arrow,
            p,
            [int(_j(p, "p_window_size", 7)), str(_j(p, "p_group_by") or "")],
        )

    def wma(p):
        return _moving_row(
            "calculate_wma",
            calculate_wma_arrow,
            p,
            [int(_j(p, "p_window_size", 7)), str(_j(p, "p_group_by") or "")],
        )

    def mv(p):
        return _moving_row(
            "calculate_moving_variance",
            calculate_moving_variance_arrow,
            p,
            [int(_j(p, "p_window_size", 7)), str(_j(p, "p_group_by") or "")],
        )

    def mq(p):
        qu = _j(p, "p_quantiles", [0.25, 0.5, 0.75])
        if isinstance(qu, str):
            qu = json.loads(qu)
        qlist = [float(x) for x in qu] if isinstance(qu, (list, tuple)) else [0.25, 0.5, 0.75]
        while len(qlist) < 3:
            qlist.append(qlist[-1] if qlist else 0.5)
        qlist = qlist[:3]
        return _ser1(
            calculate_moving_quantiles_arrow(
                pa.array([_data_json(p)]),
                pa.array([int(_j(p, "p_window_size", 7))]),
                pa.array([qlist], type=pa.list_(pa.float64())),
                pa.array([str(_j(p, "p_group_by") or "")]),
            )
        )

    def mmn(p):
        return _moving_row(
            "calculate_moving_minmax",
            calculate_moving_minmax_arrow,
            p,
            [int(_j(p, "p_window_size", 7)), str(_j(p, "p_group_by") or "")],
        )

    def mcorr(p):
        jx = _data_json({"p_data": _j(p, "p_data_x")})
        jy = _data_json({"p_data": _j(p, "p_data_y")})
        return _ser1(
            calculate_moving_correlation_arrow(
                pa.array([jx]),
                pa.array([jy]),
                pa.array([int(_j(p, "p_window_size", 7))]),
            )
        )

    def msum(p):
        return _moving_row(
            "calculate_moving_sum",
            calculate_moving_sum_arrow,
            p,
            [int(_j(p, "p_window_size", 7)), str(_j(p, "p_group_by") or "")],
        )

    def expw(p):
        return _moving_row(
            "calculate_expanding_window",
            calculate_expanding_window_arrow,
            p,
            [str(_j(p, "p_operation", "mean")), str(_j(p, "p_group_by") or "")],
        )

    def cumop(p):
        return _ser1(
            calculate_cumulative_operations_arrow(
                pa.array([_data_json(p)]),
                pa.array([json.dumps(_j(p, "p_operations", ["sum", "product", "max", "min"]))]),
                pa.array([str(_j(p, "p_group_by") or "")]),
            )
        )

    def twma(p):
        return _ser1(
            calculate_time_weighted_ma_arrow(
                pa.array([_data_json(p)]),
                pa.array([float(_j(p, "p_decay_factor", 0.1))]),
                pa.array([int(_j(p, "p_window_size", 30))]),
            )
        )

    def bb(p):
        return _ser1(
            calculate_bollinger_bands_arrow(
                pa.array([_data_json(p)]),
                pa.array([int(_j(p, "p_window_size", 20))]),
                pa.array([float(_j(p, "p_num_std", 2.0))]),
            )
        )

    def mrk(p):
        return _moving_row(
            "calculate_moving_rank",
            calculate_moving_rank_arrow,
            p,
            [int(_j(p, "p_window_size", 7)), str(_j(p, "p_group_by") or "")],
        )

    def ema(p):
        return _moving_row(
            "calculate_ema",
            calculate_ema_json_arrow,
            p,
            [float(_j(p, "p_alpha", 0.3)), str(_j(p, "p_group_by") or "")],
        )

    for name, fn in [
        ("calculate_sma", sma),
        ("calculate_wma", wma),
        ("calculate_moving_variance", mv),
        ("calculate_moving_quantiles", mq),
        ("calculate_moving_minmax", mmn),
        ("calculate_moving_correlation", mcorr),
        ("calculate_moving_sum", msum),
        ("calculate_expanding_window", expw),
        ("calculate_cumulative_operations", cumop),
        ("calculate_time_weighted_ma", twma),
        ("calculate_bollinger_bands", bb),
        ("calculate_moving_rank", mrk),
        ("calculate_ema", ema),
        ("find_correlated_metrics", _find_correlated_metrics),
        ("calculate_lag_correlation", _calculate_lag_correlation),
        ("decompose_impact_by_dimension", _decompose_impact_by_dimension),
        ("build_anomaly_explanation_payload", _build_anomaly_explanation_payload),
        ("calculate_lag", _lag_table),
        ("calculate_lead", _lead_table),
        ("aggregate_by_time", _aggregate_by_time),
        ("calculate_moving_average", _moving_average_trend),
        ("calculate_growth_rates", _growth_rates),
        ("calculate_statistical_trend", _statistical_trend),
        ("forecast_linear", _forecast_linear),
        ("calculate_volatility", _volatility),
        ("compare_periods", _compare_periods),
        ("detect_seasonality", _detect_seasonality),
        ("detect_anomalies", _detect_anomalies),
        ("get_top_metrics", _get_top_metrics),
        ("calculate_cumulative_trend", _cumulative_trend),
        ("classify_trend", _classify_trend),
        ("analyze_variance", _analyze_variance),
        ("analyze_distribution", _analyze_distribution),
        ("calculate_difference", _calculate_difference),
        ("calculate_cdf", _calculate_cdf),
        ("calculate_rolling_window", _rolling_window),
        ("calculate_autocorrelation", _autocorrelation),
        ("test_stationarity", _test_stationarity),
        ("calculate_cumulative", _calculate_cumulative_ts),
        ("calculate_percent_change", _calculate_percent_change),
        ("calculate_percent_change_comparison", _percent_change_comparison),
        ("calculate_absolute_change_comparison", _absolute_change_comparison),
        ("calculate_prepost_comparison", _prepost_comparison),
        ("calculate_stratified_analysis", _stratified_analysis),
        ("calculate_bootstrap_ci", _bootstrap_ci),
        ("calculate_power_analysis", _power_analysis),
        ("calculate_effect_sizes", _effect_sizes),
        ("adjust_pvalues_bonferroni", _bonferroni),
        ("calculate_sequential_analysis", _sequential_analysis),
        ("calculate_cuped_adjustment", _cuped_adjustment),
    ]:
        h[name] = fn
    return h
