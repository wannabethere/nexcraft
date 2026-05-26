from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pyarrow as pa

from nexcraft_jobs.compute.udfs._arrowutil import combine_chunks, corr_pearson, json_rows_from_varchar, parse_ts_value

_TS = pa.timestamp("us")


def _scalar_str(col: pa.Array | pa.ChunkedArray | None, i: int) -> str | None:
    if col is None:
        return None
    a = combine_chunks(col)
    if i >= len(a):
        return None
    v = a[i].as_py()
    if v is None:
        return None
    return str(v)


def _group_label(elem: dict[str, Any], use_group: bool) -> str:
    if not use_group:
        return "default"
    return str(elem.get("group") or elem.get("grp") or "default")


@dataclass
class RowPt:
    ts: datetime
    t64: np.datetime64
    val: float
    grp: str
    rn: int


def _sorted_points(rows: list[dict[str, Any]], use_group: bool) -> list[RowPt]:
    raw: list[RowPt] = []
    for e in rows:
        ts = parse_ts_value(e)
        if ts is None:
            continue
        v = e.get("value")
        if v is None and "metric" in e:
            v = e.get("metric")
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        g = _group_label(e, use_group)
        raw.append(RowPt(ts=ts, t64=np.datetime64(ts.replace(tzinfo=None), "us"), val=fv, grp=g, rn=0))
    raw.sort(key=lambda p: (p.ts, p.grp))
    for i, p in enumerate(raw, start=1):
        p.rn = i
    return raw


def _window_values_for_row(points: list[RowPt], idx: int, window: int) -> np.ndarray:
    """Last ``window`` rows in the same partition as ``points[idx]``, ordered by global ``rn``."""
    g = points[idx].grp
    same = [k for k in range(0, idx + 1) if points[k].grp == g]
    tail = same[-window:]
    return np.array([points[k].val for k in tail], dtype=np.float64)


def calculate_sma_arrow(
    p_data: pa.Array | pa.ChunkedArray,
    p_window_size: pa.Array | pa.ChunkedArray,
    p_group_by: pa.Array | pa.ChunkedArray | None = None,
) -> pa.Array:
    rows_batch = json_rows_from_varchar(p_data)
    wcol = combine_chunks(p_window_size)
    out: list[list[dict[str, Any]]] = []
    for bi, rows in enumerate(rows_batch):
        w = int(wcol[min(bi, len(wcol) - 1)].as_py() or 7)
        gstr = _scalar_str(p_group_by, bi)
        use_group = gstr is not None and gstr != ""
        pts = _sorted_points(rows, use_group)
        structs: list[dict[str, Any]] = []
        for j, p in enumerate(pts):
            window = _window_values_for_row(pts, j, w)
            sma = float(np.mean(window))
            std = float(np.std(window, ddof=1)) if len(window) > 1 else 0.0
            dev = float(p.val - sma)
            pct = float((dev / sma) * 100) if sma != 0 else math.nan
            ts_py = p.t64.astype(datetime)  # type: ignore[call-arg]
            structs.append(
                {
                    "row_number": p.rn,
                    "time_period": ts_py,
                    "original_value": p.val,
                    "sma_value": sma,
                    "deviation": dev,
                    "percent_deviation": pct,
                    "upper_band": sma + 2 * std,
                    "lower_band": sma - 2 * std,
                }
            )
        out.append(structs)
    st = pa.struct(
        [
            ("row_number", pa.int32()),
            ("time_period", _TS),
            ("original_value", pa.float64()),
            ("sma_value", pa.float64()),
            ("deviation", pa.float64()),
            ("percent_deviation", pa.float64()),
            ("upper_band", pa.float64()),
            ("lower_band", pa.float64()),
        ]
    )
    return pa.array(out, type=pa.list_(st))


def calculate_wma_arrow(
    p_data: pa.Array | pa.ChunkedArray,
    p_window_size: pa.Array | pa.ChunkedArray,
    p_group_by: pa.Array | pa.ChunkedArray | None = None,
) -> pa.Array:
    rows_batch = json_rows_from_varchar(p_data)
    wcol = combine_chunks(p_window_size)
    out: list[list[dict[str, Any]]] = []
    for bi, rows in enumerate(rows_batch):
        w = int(wcol[min(bi, len(wcol) - 1)].as_py() or 7)
        gstr = _scalar_str(p_group_by, bi)
        use_group = gstr is not None and gstr != ""
        pts = _sorted_points(rows, use_group)
        structs: list[dict[str, Any]] = []
        for j, p in enumerate(pts):
            start = max(0, j - w + 1)
            vals: list[float] = []
            rns: list[int] = []
            for k in range(start, j + 1):
                if pts[k].grp == p.grp:
                    vals.append(pts[k].val)
                    rns.append(pts[k].rn)
            if not vals:
                continue
            va = np.array(vals)
            ra = np.array(rns, dtype=np.float64)
            wma = float(np.dot(va, ra) / np.sum(ra))
            dev = float(p.val - wma)
            pct = float((dev / wma) * 100) if wma != 0 else math.nan
            ts_py = p.t64.astype(datetime)  # type: ignore[call-arg]
            structs.append(
                {
                    "row_number": p.rn,
                    "time_period": ts_py,
                    "original_value": p.val,
                    "wma_value": wma,
                    "deviation": dev,
                    "percent_deviation": pct,
                }
            )
        out.append(structs)
    st = pa.struct(
        [
            ("row_number", pa.int32()),
            ("time_period", _TS),
            ("original_value", pa.float64()),
            ("wma_value", pa.float64()),
            ("deviation", pa.float64()),
            ("percent_deviation", pa.float64()),
        ]
    )
    return pa.array(out, type=pa.list_(st))


def calculate_moving_variance_arrow(
    p_data: pa.Array | pa.ChunkedArray,
    p_window_size: pa.Array | pa.ChunkedArray,
    p_group_by: pa.Array | pa.ChunkedArray | None = None,
) -> pa.Array:
    rows_batch = json_rows_from_varchar(p_data)
    wcol = combine_chunks(p_window_size)
    out: list[list[dict[str, Any]]] = []
    for bi, rows in enumerate(rows_batch):
        w = int(wcol[min(bi, len(wcol) - 1)].as_py() or 7)
        gstr = _scalar_str(p_group_by, bi)
        use_group = gstr is not None and gstr != ""
        pts = _sorted_points(rows, use_group)
        structs: list[dict[str, Any]] = []
        for j, p in enumerate(pts):
            window = _window_values_for_row(pts, j, w)
            mean_val = float(np.mean(window))
            var_val = float(np.var(window, ddof=1)) if len(window) > 1 else math.nan
            std_val = float(math.sqrt(var_val)) if not math.isnan(var_val) else math.nan
            cv = float((std_val / abs(mean_val)) * 100) if mean_val != 0 and not math.isnan(std_val) else math.nan
            zs = (
                float((p.val - mean_val) / std_val)
                if not math.isnan(std_val) and std_val not in (0.0,)
                else math.nan
            )
            ts_py = p.t64.astype(datetime)  # type: ignore[call-arg]
            structs.append(
                {
                    "row_number": p.rn,
                    "time_period": ts_py,
                    "original_value": p.val,
                    "moving_mean": mean_val,
                    "moving_variance": var_val,
                    "moving_std": std_val,
                    "coefficient_variation": cv,
                    "z_score": zs,
                }
            )
        out.append(structs)
    st = pa.struct(
        [
            ("row_number", pa.int32()),
            ("time_period", _TS),
            ("original_value", pa.float64()),
            ("moving_mean", pa.float64()),
            ("moving_variance", pa.float64()),
            ("moving_std", pa.float64()),
            ("coefficient_variation", pa.float64()),
            ("z_score", pa.float64()),
        ]
    )
    return pa.array(out, type=pa.list_(st))


def calculate_moving_quantiles_arrow(
    p_data: pa.Array | pa.ChunkedArray,
    p_window_size: pa.Array | pa.ChunkedArray,
    p_quantiles: pa.Array | pa.ChunkedArray | None,
    p_group_by: pa.Array | pa.ChunkedArray | None = None,
) -> pa.Array:
    rows_batch = json_rows_from_varchar(p_data)
    wcol = combine_chunks(p_window_size)
    out: list[list[dict[str, Any]]] = []
    for bi, rows in enumerate(rows_batch):
        w = int(wcol[min(bi, len(wcol) - 1)].as_py() or 7)
        if p_quantiles is None:
            qlist = [0.25, 0.5, 0.75]
        else:
            qa = combine_chunks(p_quantiles)
            cell = qa[min(bi, len(qa) - 1)].as_py()
            if isinstance(cell, (list, tuple)) and len(cell) >= 3:
                qlist = [float(x) for x in cell[:3]]
            else:
                qlist = [0.25, 0.5, 0.75]
        gstr = _scalar_str(p_group_by, bi)
        use_group = gstr is not None and gstr != ""
        pts = _sorted_points(rows, use_group)
        structs: list[dict[str, Any]] = []
        for j, p in enumerate(pts):
            window = _window_values_for_row(pts, j, w)
            q25 = float(np.quantile(window, qlist[0]))
            q50 = float(np.quantile(window, qlist[1]))
            q75 = float(np.quantile(window, qlist[2]))
            ts_py = p.t64.astype(datetime)  # type: ignore[call-arg]
            structs.append(
                {
                    "row_number": p.rn,
                    "time_period": ts_py,
                    "original_value": p.val,
                    "q25": q25,
                    "q50_median": q50,
                    "q75": q75,
                    "iqr": q75 - q25,
                }
            )
        out.append(structs)
    st = pa.struct(
        [
            ("row_number", pa.int32()),
            ("time_period", _TS),
            ("original_value", pa.float64()),
            ("q25", pa.float64()),
            ("q50_median", pa.float64()),
            ("q75", pa.float64()),
            ("iqr", pa.float64()),
        ]
    )
    return pa.array(out, type=pa.list_(st))


def calculate_moving_minmax_arrow(
    p_data: pa.Array | pa.ChunkedArray,
    p_window_size: pa.Array | pa.ChunkedArray,
    p_group_by: pa.Array | pa.ChunkedArray | None = None,
) -> pa.Array:
    rows_batch = json_rows_from_varchar(p_data)
    wcol = combine_chunks(p_window_size)
    out: list[list[dict[str, Any]]] = []
    for bi, rows in enumerate(rows_batch):
        w = int(wcol[min(bi, len(wcol) - 1)].as_py() or 7)
        gstr = _scalar_str(p_group_by, bi)
        use_group = gstr is not None and gstr != ""
        pts = _sorted_points(rows, use_group)
        structs: list[dict[str, Any]] = []
        for j, p in enumerate(pts):
            window = _window_values_for_row(pts, j, w)
            mn = float(np.min(window))
            mx = float(np.max(window))
            rng = mx - mn
            pos = float((p.val - mn) / rng) if rng != 0 else 0.5
            ts_py = p.t64.astype(datetime)  # type: ignore[call-arg]
            structs.append(
                {
                    "row_number": p.rn,
                    "time_period": ts_py,
                    "original_value": p.val,
                    "moving_min": mn,
                    "moving_max": mx,
                    "moving_range": rng,
                    "position_in_range": pos,
                }
            )
        out.append(structs)
    st = pa.struct(
        [
            ("row_number", pa.int32()),
            ("time_period", _TS),
            ("original_value", pa.float64()),
            ("moving_min", pa.float64()),
            ("moving_max", pa.float64()),
            ("moving_range", pa.float64()),
            ("position_in_range", pa.float64()),
        ]
    )
    return pa.array(out, type=pa.list_(st))


def calculate_moving_correlation_arrow(
    p_data_x: pa.Array | pa.ChunkedArray,
    p_data_y: pa.Array | pa.ChunkedArray,
    p_window_size: pa.Array | pa.ChunkedArray,
) -> pa.Array:
    xb = json_rows_from_varchar(p_data_x)
    yb = json_rows_from_varchar(p_data_y)
    wcol = combine_chunks(p_window_size)
    out: list[list[dict[str, Any]]] = []
    for bi in range(max(len(xb), len(yb))):
        rows_x = xb[bi] if bi < len(xb) else []
        rows_y = yb[bi] if bi < len(yb) else []
        w = int(wcol[min(bi, len(wcol) - 1)].as_py() or 7)
        pts_x = _sorted_points(rows_x, False)
        pts_y = _sorted_points(rows_y, False)
        n = min(len(pts_x), len(pts_y))
        structs: list[dict[str, Any]] = []
        for j in range(n):
            lo = max(0, j - w + 1)
            vx = np.array([pts_x[k].val for k in range(lo, j + 1)], dtype=np.float64)
            vy = np.array([pts_y[k].val for k in range(lo, j + 1)], dtype=np.float64)
            c = corr_pearson(vx, vy)
            if math.isnan(c):
                strength = "very_weak"
            else:
                a = abs(c)
                if a > 0.8:
                    strength = "very_strong"
                elif a > 0.6:
                    strength = "strong"
                elif a > 0.4:
                    strength = "moderate"
                elif a > 0.2:
                    strength = "weak"
                else:
                    strength = "very_weak"
            ts_py = pts_x[j].t64.astype(datetime)  # type: ignore[call-arg]
            structs.append(
                {
                    "row_number": j + 1,
                    "time_period": ts_py,
                    "value_x": pts_x[j].val,
                    "value_y": pts_y[j].val,
                    "correlation": float(c) if not math.isnan(c) else math.nan,
                    "correlation_strength": strength,
                }
            )
        out.append(structs)
    st = pa.struct(
        [
            ("row_number", pa.int32()),
            ("time_period", _TS),
            ("value_x", pa.float64()),
            ("value_y", pa.float64()),
            ("correlation", pa.float64()),
            ("correlation_strength", pa.string()),
        ]
    )
    return pa.array(out, type=pa.list_(st))


def calculate_moving_sum_arrow(
    p_data: pa.Array | pa.ChunkedArray,
    p_window_size: pa.Array | pa.ChunkedArray,
    p_group_by: pa.Array | pa.ChunkedArray | None = None,
) -> pa.Array:
    rows_batch = json_rows_from_varchar(p_data)
    wcol = combine_chunks(p_window_size)
    out: list[list[dict[str, Any]]] = []
    for bi, rows in enumerate(rows_batch):
        w = int(wcol[min(bi, len(wcol) - 1)].as_py() or 7)
        gstr = _scalar_str(p_group_by, bi)
        use_group = gstr is not None and gstr != ""
        pts = _sorted_points(rows, use_group)
        structs: list[dict[str, Any]] = []
        for j, p in enumerate(pts):
            window = _window_values_for_row(pts, j, w)
            s = float(np.sum(window))
            pct = float((p.val / s) * 100) if s != 0 else math.nan
            ts_py = p.t64.astype(datetime)  # type: ignore[call-arg]
            structs.append(
                {
                    "row_number": p.rn,
                    "time_period": ts_py,
                    "original_value": p.val,
                    "moving_sum": s,
                    "contribution_pct": pct,
                }
            )
        out.append(structs)
    st = pa.struct(
        [
            ("row_number", pa.int32()),
            ("time_period", _TS),
            ("original_value", pa.float64()),
            ("moving_sum", pa.float64()),
            ("contribution_pct", pa.float64()),
        ]
    )
    return pa.array(out, type=pa.list_(st))


def _expanding_window_values(pts: list[RowPt], j: int) -> np.ndarray:
    g = pts[j].grp
    return np.array([pts[k].val for k in range(0, j + 1) if pts[k].grp == g], dtype=np.float64)


def calculate_expanding_window_arrow(
    p_data: pa.Array | pa.ChunkedArray,
    p_operation: pa.Array | pa.ChunkedArray,
    p_group_by: pa.Array | pa.ChunkedArray | None = None,
) -> pa.Array:
    rows_batch = json_rows_from_varchar(p_data)
    opcol = combine_chunks(p_operation)
    out: list[list[dict[str, Any]]] = []
    for bi, rows in enumerate(rows_batch):
        op = (str(opcol[min(bi, len(opcol) - 1)].as_py()) or "mean").lower()
        gstr = _scalar_str(p_group_by, bi)
        use_group = gstr is not None and gstr != ""
        pts = _sorted_points(rows, use_group)
        structs: list[dict[str, Any]] = []
        for j, p in enumerate(pts):
            window = _expanding_window_values(pts, j)
            if op == "sum":
                ev = float(np.sum(window))
            elif op == "std":
                ev = float(np.std(window, ddof=1)) if len(window) > 1 else math.nan
            elif op == "min":
                ev = float(np.min(window))
            elif op == "max":
                ev = float(np.max(window))
            elif op == "count":
                ev = float(len(window))
            else:
                ev = float(np.mean(window))
            ts_py = p.t64.astype(datetime)  # type: ignore[call-arg]
            structs.append(
                {
                    "row_number": p.rn,
                    "time_period": ts_py,
                    "original_value": p.val,
                    "expanding_value": ev,
                    "window_size": int(len(window)),
                }
            )
        out.append(structs)
    st = pa.struct(
        [
            ("row_number", pa.int32()),
            ("time_period", _TS),
            ("original_value", pa.float64()),
            ("expanding_value", pa.float64()),
            ("window_size", pa.int32()),
        ]
    )
    return pa.array(out, type=pa.list_(st))


def calculate_cumulative_operations_arrow(
    p_data: pa.Array | pa.ChunkedArray,
    p_operations: pa.Array | pa.ChunkedArray | None,
    p_group_by: pa.Array | pa.ChunkedArray | None = None,
) -> pa.Array:
    rows_batch = json_rows_from_varchar(p_data)
    out: list[list[dict[str, Any]]] = []
    for bi, rows in enumerate(rows_batch):
        gstr = _scalar_str(p_group_by, bi)
        use_group = gstr is not None and gstr != ""
        pts = _sorted_points(rows, use_group)
        total_by_g: dict[str, float] = {}
        for p in pts:
            total_by_g[p.grp] = total_by_g.get(p.grp, 0.0) + p.val
        csum: dict[str, float] = {}
        cprod: dict[str, float | None] = {}
        cmax: dict[str, float] = {}
        cmin: dict[str, float] = {}
        structs: list[dict[str, Any]] = []
        for p in pts:
            g = p.grp
            csum[g] = csum.get(g, 0.0) + p.val
            if g not in cprod or cprod[g] is None:
                cprod[g] = p.val
            else:
                cprod[g] = float(cprod[g]) * p.val
            if g not in cmax:
                cmax[g] = p.val
            else:
                cmax[g] = max(cmax[g], p.val)
            if g not in cmin:
                cmin[g] = p.val
            else:
                cmin[g] = min(cmin[g], p.val)
            tot = total_by_g.get(g, 0.0)
            pct = float((csum[g] / tot) * 100) if tot != 0 else math.nan
            ts_py = p.t64.astype(datetime)  # type: ignore[call-arg]
            structs.append(
                {
                    "row_number": p.rn,
                    "time_period": ts_py,
                    "original_value": p.val,
                    "cumsum": csum[g],
                    "cumproduct": float(cprod[g] or 0.0),
                    "cummax": cmax[g],
                    "cummin": cmin[g],
                    "percent_of_total": pct,
                }
            )
        out.append(structs)
    st = pa.struct(
        [
            ("row_number", pa.int32()),
            ("time_period", _TS),
            ("original_value", pa.float64()),
            ("cumsum", pa.float64()),
            ("cumproduct", pa.float64()),
            ("cummax", pa.float64()),
            ("cummin", pa.float64()),
            ("percent_of_total", pa.float64()),
        ]
    )
    return pa.array(out, type=pa.list_(st))


def calculate_time_weighted_ma_arrow(
    p_data: pa.Array | pa.ChunkedArray,
    p_decay_factor: pa.Array | pa.ChunkedArray,
    p_window_size: pa.Array | pa.ChunkedArray,
) -> pa.Array:
    rows_batch = json_rows_from_varchar(p_data)
    dcol = combine_chunks(p_decay_factor)
    wcol = combine_chunks(p_window_size)
    out: list[list[dict[str, Any]]] = []
    for bi, rows in enumerate(rows_batch):
        decay = float(dcol[min(bi, len(dcol) - 1)].as_py() or 0.1)
        win = int(wcol[min(bi, len(wcol) - 1)].as_py() or 30)
        pts = _sorted_points(rows, False)
        structs: list[dict[str, Any]] = []
        for j, p in enumerate(pts):
            num = 0.0
            den = 0.0
            for q in pts:
                if q.rn > p.rn - win and q.rn <= p.rn:
                    wgt = math.exp(-decay * (p.rn - q.rn))
                    num += q.val * wgt
                    den += wgt
            tw = float(num / den) if den != 0 else p.val
            ts_py = p.t64.astype(datetime)  # type: ignore[call-arg]
            structs.append(
                {
                    "row_number": p.rn,
                    "time_period": ts_py,
                    "original_value": p.val,
                    "twma_value": tw,
                    "deviation": float(p.val - tw),
                }
            )
        out.append(structs)
    st = pa.struct(
        [
            ("row_number", pa.int32()),
            ("time_period", _TS),
            ("original_value", pa.float64()),
            ("twma_value", pa.float64()),
            ("deviation", pa.float64()),
        ]
    )
    return pa.array(out, type=pa.list_(st))


def calculate_bollinger_bands_arrow(
    p_data: pa.Array | pa.ChunkedArray,
    p_window_size: pa.Array | pa.ChunkedArray,
    p_num_std: pa.Array | pa.ChunkedArray,
) -> pa.Array:
    rows_batch = json_rows_from_varchar(p_data)
    wcol = combine_chunks(p_window_size)
    ncol = combine_chunks(p_num_std)
    out: list[list[dict[str, Any]]] = []
    for bi, rows in enumerate(rows_batch):
        w = int(wcol[min(bi, len(wcol) - 1)].as_py() or 20)
        nstd = float(ncol[min(bi, len(ncol) - 1)].as_py() or 2.0)
        pts = _sorted_points(rows, False)
        structs: list[dict[str, Any]] = []
        for j, p in enumerate(pts):
            window = _window_values_for_row(pts, j, w)
            sma = float(np.mean(window))
            sd = float(np.std(window, ddof=1)) if len(window) > 1 else 0.0
            upper = sma + nstd * sd
            lower = sma - nstd * sd
            bw = float((upper - lower) / sma) if sma != 0 else math.nan
            pb = float((p.val - lower) / (upper - lower)) if upper != lower else math.nan
            ts_py = p.t64.astype(datetime)  # type: ignore[call-arg]
            structs.append(
                {
                    "row_number": p.rn,
                    "time_period": ts_py,
                    "original_value": p.val,
                    "middle_band": sma,
                    "upper_band": upper,
                    "lower_band": lower,
                    "bandwidth": bw,
                    "percent_b": pb,
                }
            )
        out.append(structs)
    st = pa.struct(
        [
            ("row_number", pa.int32()),
            ("time_period", _TS),
            ("original_value", pa.float64()),
            ("middle_band", pa.float64()),
            ("upper_band", pa.float64()),
            ("lower_band", pa.float64()),
            ("bandwidth", pa.float64()),
            ("percent_b", pa.float64()),
        ]
    )
    return pa.array(out, type=pa.list_(st))


def calculate_moving_rank_arrow(
    p_data: pa.Array | pa.ChunkedArray,
    p_window_size: pa.Array | pa.ChunkedArray,
    p_group_by: pa.Array | pa.ChunkedArray | None = None,
) -> pa.Array:
    rows_batch = json_rows_from_varchar(p_data)
    wcol = combine_chunks(p_window_size)
    out: list[list[dict[str, Any]]] = []
    for bi, rows in enumerate(rows_batch):
        w = int(wcol[min(bi, len(wcol) - 1)].as_py() or 7)
        gstr = _scalar_str(p_group_by, bi)
        use_group = gstr is not None and gstr != ""
        pts = _sorted_points(rows, use_group)
        structs: list[dict[str, Any]] = []
        for j, p in enumerate(pts):
            window = _window_values_for_row(pts, j, w)
            n = len(window)
            wr = int(np.sum(window > p.val) + 1)
            if n > 1:
                pct = float(np.sum(window < p.val) / (n - 1)) * 100.0
            else:
                pct = 0.0
            mx = float(np.max(window))
            mn = float(np.min(window))
            ts_py = p.t64.astype(datetime)  # type: ignore[call-arg]
            structs.append(
                {
                    "row_number": p.rn,
                    "time_period": ts_py,
                    "original_value": p.val,
                    "window_rank": wr,
                    "window_percentile": pct,
                    "is_highest": bool(abs(p.val - mx) < 1e-12),
                    "is_lowest": bool(abs(p.val - mn) < 1e-12),
                }
            )
        out.append(structs)
    st = pa.struct(
        [
            ("row_number", pa.int32()),
            ("time_period", _TS),
            ("original_value", pa.float64()),
            ("window_rank", pa.int32()),
            ("window_percentile", pa.float64()),
            ("is_highest", pa.bool_()),
            ("is_lowest", pa.bool_()),
        ]
    )
    return pa.array(out, type=pa.list_(st))


def calculate_ema_json_arrow(
    p_data: pa.Array | pa.ChunkedArray,
    p_alpha: pa.Array | pa.ChunkedArray,
    p_group_by: pa.Array | pa.ChunkedArray | None = None,
) -> pa.Array:
    """PostgreSQL ``calculate_ema`` on JSON arrays (``p_data`` as VARCHAR JSON)."""
    rows_batch = json_rows_from_varchar(p_data)
    acol = combine_chunks(p_alpha)
    out: list[list[dict[str, Any]]] = []
    for bi, rows in enumerate(rows_batch):
        alpha = float(acol[min(bi, len(acol) - 1)].as_py() or 0.3)
        gstr = _scalar_str(p_group_by, bi)
        use_group = gstr is not None and gstr != ""
        pts = _sorted_points(rows, use_group)
        structs: list[dict[str, Any]] = []
        last_ema: dict[str, float] = {}
        for p in pts:
            g = p.grp
            if g not in last_ema:
                ema_v = p.val
            else:
                ema_v = alpha * p.val + (1.0 - alpha) * last_ema[g]
            last_ema[g] = ema_v
            ts_py = p.t64.astype(datetime)  # type: ignore[call-arg]
            structs.append(
                {
                    "row_number": p.rn,
                    "time_period": ts_py,
                    "original_value": p.val,
                    "ema_value": ema_v,
                    "deviation": float(p.val - ema_v),
                }
            )
        out.append(structs)
    st = pa.struct(
        [
            ("row_number", pa.int32()),
            ("time_period", _TS),
            ("original_value", pa.float64()),
            ("ema_value", pa.float64()),
            ("deviation", pa.float64()),
        ]
    )
    return pa.array(out, type=pa.list_(st))
