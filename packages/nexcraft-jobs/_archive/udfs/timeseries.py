from __future__ import annotations

import numpy as np
import pyarrow as pa


def _combine_chunks(col: pa.Array | pa.ChunkedArray) -> pa.Array:
    if isinstance(col, pa.ChunkedArray):
        return col.combine_chunks()
    return col


def ema_arrow(values, alpha) -> pa.Array:
    """EMA per list row (DuckDB Arrow UDF — inputs are chunked Arrow columns)."""
    values_arr = _combine_chunks(values)
    alpha_arr = _combine_chunks(alpha)
    a = float(alpha_arr[0].as_py())
    out: list[list[float]] = []
    for lst in values_arr.to_pylist():
        if not lst:
            out.append([])
            continue
        arr = np.asarray(lst, dtype=np.float64)
        if arr.size == 0:
            out.append([])
            continue
        e = np.empty_like(arr)
        e[0] = arr[0]
        for j in range(1, len(arr)):
            e[j] = a * arr[j] + (1.0 - a) * e[j - 1]
        out.append(e.tolist())
    return pa.array(out, type=pa.list_(pa.float64()))


def stl_decompose_arrow(values, period) -> pa.StructArray:
    from statsmodels.tsa.seasonal import STL

    values_arr = _combine_chunks(values)
    period_arr = _combine_chunks(period)
    p = int(period_arr[0].as_py())
    trends: list[list[float]] = []
    seasons: list[list[float]] = []
    resids: list[list[float]] = []
    for lst in values_arr.to_pylist():
        if not lst:
            trends.append([])
            seasons.append([])
            resids.append([])
            continue
        arr = np.asarray(lst, dtype=np.float64)
        if arr.size == 0:
            trends.append([])
            seasons.append([])
            resids.append([])
            continue
        res = STL(arr, period=p).fit()
        trends.append(list(map(float, res.trend)))
        seasons.append(list(map(float, res.seasonal)))
        resids.append(list(map(float, res.resid)))
    return pa.StructArray.from_arrays(
        [
            pa.array(trends, type=pa.list_(pa.float64())),
            pa.array(seasons, type=pa.list_(pa.float64())),
            pa.array(resids, type=pa.list_(pa.float64())),
        ],
        names=["trend", "seasonal", "resid"],
    )
