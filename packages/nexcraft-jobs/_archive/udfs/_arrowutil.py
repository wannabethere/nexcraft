from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import numpy as np
import pyarrow as pa


def combine_chunks(col: pa.Array | pa.ChunkedArray) -> pa.Array:
    if isinstance(col, pa.ChunkedArray):
        return col.combine_chunks()
    return col


def json_rows_from_varchar(col: pa.Array | pa.ChunkedArray) -> list[list[dict[str, Any]]]:
    """Each cell: JSON array of objects (PostgreSQL jsonb_array_elements shape)."""
    arr = combine_chunks(col)
    out: list[list[dict[str, Any]]] = []
    for raw in arr.to_pylist():
        if raw is None:
            out.append([])
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str) or not raw.strip():
            out.append([])
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            out.append([])
            continue
        if isinstance(data, list):
            out.append([x for x in data if isinstance(x, dict)])
        else:
            out.append([])
    return out


def parse_ts_value(elem: dict[str, Any], time_key: str = "time") -> datetime | None:
    v = elem.get(time_key)
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        s = v.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    return None


def corr_pearson(a: np.ndarray, b: np.ndarray) -> float:
    if a.size < 2 or b.size != a.size:
        return float("nan")
    if np.allclose(a, a[0]) or np.allclose(b, b[0]):
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])
