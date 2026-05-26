from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import pyarrow as pa

from nexcraft_jobs.compute.udfs._arrowutil import combine_chunks


def _py_ts(v: Any) -> str:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return datetime.combine(v, datetime.min.time()).isoformat()
    return str(v)


def list_timeseries_to_json_arrow(
    times: pa.Array | pa.ChunkedArray,
    values: pa.Array | pa.ChunkedArray,
) -> pa.Array:
    """
    After ``GROUP BY``, pass ``list(ts ORDER BY ts)`` and ``list(val ORDER BY ts)`` so each row
    becomes the JSON array ``sql_functions.json`` expects for ``p_data`` (``time`` + ``value``).
    """
    ta = combine_chunks(times)
    va = combine_chunks(values)
    out: list[str] = []
    for i in range(len(ta)):
        tcell = ta[i]
        vcell = va[i]
        pairs: list[dict[str, Any]] = []
        tlist = tcell.as_py()
        vlist = vcell.as_py()
        if not isinstance(tlist, (list, tuple)) or not isinstance(vlist, (list, tuple)):
            out.append("[]")
            continue
        n = min(len(tlist), len(vlist))
        for j in range(n):
            try:
                fv = float(vlist[j])
            except (TypeError, ValueError):
                continue
            pairs.append({"time": _py_ts(tlist[j]), "value": fv})
        out.append(json.dumps(pairs))
    return pa.array(out, type=pa.string())


def list_timeseries_group_to_json_arrow(
    times: pa.Array | pa.ChunkedArray,
    values: pa.Array | pa.ChunkedArray,
    groups: pa.Array | pa.ChunkedArray,
) -> pa.Array:
    """Same as ``list_timeseries_to_json_arrow`` but adds ``group`` on each point for ``p_group_by`` flows."""
    ta = combine_chunks(times)
    va = combine_chunks(values)
    ga = combine_chunks(groups)
    out: list[str] = []
    for i in range(len(ta)):
        tlist = ta[i].as_py()
        vlist = va[i].as_py()
        glist = ga[i].as_py()
        if (
            not isinstance(tlist, (list, tuple))
            or not isinstance(vlist, (list, tuple))
            or not isinstance(glist, (list, tuple))
        ):
            out.append("[]")
            continue
        n = min(len(tlist), len(vlist), len(glist))
        pairs: list[dict[str, Any]] = []
        for j in range(n):
            try:
                fv = float(vlist[j])
            except (TypeError, ValueError):
                continue
            pairs.append(
                {
                    "time": _py_ts(tlist[j]),
                    "value": fv,
                    "group": str(glist[j]) if glist[j] is not None else "",
                }
            )
        out.append(json.dumps(pairs))
    return pa.array(out, type=pa.string())


def list_metric_series_to_json_arrow(
    times: pa.Array | pa.ChunkedArray,
    metrics: pa.Array | pa.ChunkedArray,
) -> pa.Array:
    """Trend helpers use ``metric`` instead of ``value`` (see ``trend_analysis_functions.sql``)."""
    ta = combine_chunks(times)
    ma = combine_chunks(metrics)
    out: list[str] = []
    for i in range(len(ta)):
        tlist = ta[i].as_py()
        mlist = ma[i].as_py()
        if not isinstance(tlist, (list, tuple)) or not isinstance(mlist, (list, tuple)):
            out.append("[]")
            continue
        n = min(len(tlist), len(mlist))
        pairs: list[dict[str, Any]] = []
        for j in range(n):
            try:
                fm = float(mlist[j])
            except (TypeError, ValueError):
                continue
            pairs.append({"time": _py_ts(tlist[j]), "metric": fm})
        out.append(json.dumps(pairs))
    return pa.array(out, type=pa.string())


def register_list_helpers_udfs(con: Any) -> None:
    import duckdb
    import duckdb.func

    arrow = duckdb.func.PythonUDFType.ARROW
    con.create_function(
        "list_timeseries_to_json",
        list_timeseries_to_json_arrow,
        ["TIMESTAMP[]", "DOUBLE[]"],
        "VARCHAR",
        type=arrow,
    )
    con.create_function(
        "list_timeseries_group_to_json",
        list_timeseries_group_to_json_arrow,
        ["TIMESTAMP[]", "DOUBLE[]", "VARCHAR[]"],
        "VARCHAR",
        type=arrow,
    )
    con.create_function(
        "list_metric_series_to_json",
        list_metric_series_to_json_arrow,
        ["TIMESTAMP[]", "DOUBLE[]"],
        "VARCHAR",
        type=arrow,
    )
