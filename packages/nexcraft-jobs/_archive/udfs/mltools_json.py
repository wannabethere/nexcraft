from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pyarrow as pa

from nexcraft_jobs.compute.udfs._arrowutil import combine_chunks, parse_ts_value


def _loads(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def ml_funnel_json_arrow(
    events_json: pa.Array | pa.ChunkedArray,
    event_column: pa.Array | pa.ChunkedArray,
    user_id_column: pa.Array | pa.ChunkedArray,
    steps_json: pa.Array | pa.ChunkedArray,
) -> pa.Array:
    """
    JSON summary aligned with ``funnelanalysis.analyze_funnel`` overall output:
    ``events_json`` = array of ``{event_col, user_id_col, ...}``.
    ``steps_json`` = JSON array of step event names in order.
    """
    ej = combine_chunks(events_json)
    ec = combine_chunks(event_column)
    uc = combine_chunks(user_id_column)
    sj = combine_chunks(steps_json)
    out: list[str] = []
    for bi in range(len(ej)):
        ev = _loads(ej[bi].as_py())
        ecol = str(ec[min(bi, len(ec) - 1)].as_py() or "event")
        ucol = str(uc[min(bi, len(uc) - 1)].as_py() or "user_id")
        steps_raw = sj[bi].as_py()
        steps = _loads(steps_raw) if isinstance(steps_raw, str) else steps_raw
        if not isinstance(steps, list):
            steps = []
        rows = ev if isinstance(ev, list) else []
        users_by_step: list[set[str]] = []
        for step in steps:
            sset: set[str] = set()
            for r in rows:
                if not isinstance(r, dict):
                    continue
                if str(r.get(ecol)) == str(step):
                    uid = r.get(ucol)
                    if uid is not None:
                        sset.add(str(uid))
            users_by_step.append(sset)
        counts = [len(s) for s in users_by_step]
        conv: list[float] = []
        cum: list[float] = []
        for i, c in enumerate(counts):
            if i == 0:
                conv.append(1.0)
                cum.append(1.0)
            else:
                prev = counts[i - 1] or 0
                conv.append((c / prev) if prev else 0.0)
                base = counts[0] or 0
                cum.append((c / base) if base else 0.0)
        summary = {
            "step": [str(s) for s in steps],
            "count": counts,
            "conversion_rate": [round(x, 6) for x in conv],
            "cumulative_rate": [round(x, 6) for x in cum],
        }
        out.append(json.dumps(summary))
    return pa.array(out, type=pa.string())


def ml_cohort_retention_json_arrow(
    events_json: pa.Array | pa.ChunkedArray,
    user_col: pa.Array | pa.ChunkedArray,
    cohort_col: pa.Array | pa.ChunkedArray,
    period_col: pa.Array | pa.ChunkedArray,
) -> pa.Array:
    """
    Lightweight cohort-style retention: ``events_json`` rows with ``user_col``,
    ``cohort_col`` (first activity bucket), ``period_col`` (int period index).
    Returns JSON ``{cohorts: [...]}``.
    """
    ej = combine_chunks(events_json)
    uc = combine_chunks(user_col)
    cc = combine_chunks(cohort_col)
    pc = combine_chunks(period_col)
    out: list[str] = []
    for bi in range(len(ej)):
        ev = _loads(ej[bi].as_py())
        ucn = str(uc[min(bi, len(uc) - 1)].as_py() or "user_id")
        ccn = str(cc[min(bi, len(cc) - 1)].as_py() or "cohort")
        pcn = str(pc[min(bi, len(pc) - 1)].as_py() or "period")
        rows = ev if isinstance(ev, list) else []
        cohort_users: dict[str, set[str]] = {}
        active: dict[tuple[str, int], set[str]] = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            u = r.get(ucn)
            c = str(r.get(ccn) or "")
            try:
                p = int(r.get(pcn) or 0)
            except (TypeError, ValueError):
                p = 0
            if u is None:
                continue
            us = str(u)
            cohort_users.setdefault(c, set()).add(us)
            active.setdefault((c, p), set()).add(us)
        result: list[dict[str, Any]] = []
        for cohort, base in sorted(cohort_users.items()):
            base_n = len(base) or 1
            periods_list: list[dict[str, Any]] = []
            max_p = max((p for (c, p) in active if c == cohort), default=0)
            for p in range(0, max_p + 1):
                ret = len(active.get((cohort, p), set()) & base)
                periods_list.append(
                    {"period": p, "retained": ret, "rate": round(ret / base_n, 6)}
                )
            result.append({"cohort": cohort, "periods": periods_list})
        out.append(json.dumps({"cohorts": result}))
    return pa.array(out, type=pa.string())


def ml_metrics_summary_json_arrow(values_json: pa.Array | pa.ChunkedArray) -> pa.Array:
    """``values_json`` = JSON array of numbers (``metrics_tools``-style descriptive stats)."""
    arr = combine_chunks(values_json)
    out: list[str] = []
    for i in range(len(arr)):
        v = _loads(arr[i].as_py())
        nums: list[float] = []
        if isinstance(v, list):
            for x in v:
                try:
                    nums.append(float(x))
                except (TypeError, ValueError):
                    continue
        a = np.array(nums, dtype=np.float64)
        if a.size == 0:
            out.append(json.dumps({"count": 0}))
            continue
        summary = {
            "count": int(a.size),
            "mean": float(np.mean(a)),
            "std": float(np.std(a, ddof=1)) if a.size > 1 else 0.0,
            "min": float(np.min(a)),
            "max": float(np.max(a)),
            "median": float(np.median(a)),
        }
        out.append(
            json.dumps(
                {k: (round(v, 6) if isinstance(v, float) else v) for k, v in summary.items()}
            )
        )
    return pa.array(out, type=pa.string())


def _kmeans(x: np.ndarray, k: int, iters: int = 30) -> tuple[np.ndarray, np.ndarray]:
    n, _d = x.shape
    if n == 0 or k <= 0:
        return np.array([]), x
    rng = np.random.default_rng(42)
    idx = rng.choice(n, size=min(k, n), replace=False)
    centers = x[idx].copy()
    for _ in range(iters):
        dist = np.linalg.norm(x[:, None, :] - centers[None, :, :], axis=2)
        labels = np.argmin(dist, axis=1)
        for j in range(centers.shape[0]):
            mask = labels == j
            if np.any(mask):
                centers[j] = x[mask].mean(axis=0)
    dist = np.linalg.norm(x[:, None, :] - centers[None, :, :], axis=2)
    labels = np.argmin(dist, axis=1)
    return labels, centers


def ml_segment_kmeans_json_arrow(
    rows_json: pa.Array | pa.ChunkedArray,
    k: pa.Array | pa.ChunkedArray,
    feature_keys_json: pa.Array | pa.ChunkedArray,
) -> pa.Array:
    """
    ``segmentationtools``-style KMeans on numeric features: ``rows_json`` array of objects,
    ``feature_keys_json`` JSON array of keys to use as dimensions.
    """
    rj = combine_chunks(rows_json)
    kj = combine_chunks(k)
    fk = combine_chunks(feature_keys_json)
    out: list[str] = []
    for bi in range(len(rj)):
        rows = _loads(rj[bi].as_py())
        kk = int(kj[min(bi, len(kj) - 1)].as_py() or 3)
        keys_raw = fk[bi].as_py()
        keys = _loads(keys_raw) if isinstance(keys_raw, str) else keys_raw
        if not isinstance(keys, list):
            keys = []
        keys = [str(k) for k in keys]
        mat: list[list[float]] = []
        if isinstance(rows, list):
            for r in rows:
                if not isinstance(r, dict):
                    continue
                rowv: list[float] = []
                ok = True
                for ky in keys:
                    if ky not in r:
                        ok = False
                        break
                    try:
                        rowv.append(float(r[ky]))
                    except (TypeError, ValueError):
                        ok = False
                        break
                if ok:
                    mat.append(rowv)
        x = np.array(mat, dtype=np.float64)
        if x.size == 0 or not keys:
            out.append(json.dumps({"labels": [], "centers": [], "k": kk}))
            continue
        labels, centers = _kmeans(x, min(kk, x.shape[0]))
        out.append(
            json.dumps(
                {
                    "labels": labels.tolist(),
                    "centers": centers.round(6).tolist(),
                    "k": int(min(kk, x.shape[0])),
                    "feature_keys": keys,
                }
            )
        )
    return pa.array(out, type=pa.string())


def ml_trend_linear_json_arrow(series_json: pa.Array | pa.ChunkedArray) -> pa.Array:
    """
    Linear trend on ``[{time, metric}]`` — minimal ``TrendPipe`` / statistical trend style
    (slope, intercept, r_squared on index axis).
    """
    arr = combine_chunks(series_json)
    out: list[str] = []
    for i in range(len(arr)):
        raw = _loads(arr[i].as_py())
        ys_list: list[float] = []
        if isinstance(raw, list):
            for e in raw:
                if not isinstance(e, dict):
                    continue
                ts = parse_ts_value(e, "time")
                if ts is None:
                    continue
                try:
                    y = float(e.get("metric", e.get("value")))
                except (TypeError, ValueError):
                    continue
                ys_list.append(y)
        ys = np.array(ys_list, dtype=np.float64)
        if ys.size < 2:
            out.append(json.dumps({"slope": None, "r_squared": None, "n": int(ys.size)}))
            continue
        t = np.arange(ys.size, dtype=np.float64)
        slope, intercept = np.polyfit(t, ys, 1)
        y_hat = slope * t + intercept
        ss_res = float(np.sum((ys - y_hat) ** 2))
        ss_tot = float(np.sum((ys - ys.mean()) ** 2)) or 1.0
        r2 = 1.0 - ss_res / ss_tot
        out.append(
            json.dumps(
                {
                    "slope": round(float(slope), 8),
                    "intercept": round(float(intercept), 8),
                    "r_squared": round(float(r2), 6),
                    "n": int(ys.size),
                    "computed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
    return pa.array(out, type=pa.string())
