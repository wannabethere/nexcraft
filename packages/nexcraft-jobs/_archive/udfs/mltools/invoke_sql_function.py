from __future__ import annotations

import json
from typing import Any

import duckdb
import duckdb.func
import pyarrow as pa

from nexcraft_jobs.compute.udfs._arrowutil import combine_chunks
from nexcraft_jobs.compute.udfs.mltools.sql_invoke_handlers import build_handlers

_handlers: dict[str, Any] | None = None


def _get_handlers() -> dict[str, Any]:
    global _handlers
    if _handlers is None:
        _handlers = build_handlers()
    return _handlers


def _loads_payload(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        o = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return o if isinstance(o, dict) else {}


def invoke_sql_function_arrow(
    function_name: pa.Array | pa.ChunkedArray,
    payload: pa.Array | pa.ChunkedArray,
) -> pa.Array:
    """Dispatch any name in ``sql_functions.json`` ``function_reference`` via a single VARCHAR result."""
    fnames = combine_chunks(function_name)
    pays = combine_chunks(payload)
    handlers = _get_handlers()
    out: list[str] = []
    for i in range(len(fnames)):
        name = str(fnames[i].as_py() or "")
        raw = pays[i].as_py() if i < len(pays) else "{}"
        p = _loads_payload(raw)
        p["function_name"] = name
        fn = handlers.get(name)
        if fn is None:
            out.append(json.dumps({"error": "unknown_function", "function_name": name}))
            continue
        try:
            out.append(fn(p))
        except (TypeError, ValueError, KeyError, ZeroDivisionError, json.JSONDecodeError) as e:
            out.append(
                json.dumps(
                    {"error": type(e).__name__, "message": str(e), "function_name": name},
                    default=str,
                )
            )
    return pa.array(out, type=pa.string())


def register_invoke_sql_udfs(con: duckdb.DuckDBPyConnection) -> None:
    con.create_function(
        "invoke_sql_function",
        invoke_sql_function_arrow,
        ["VARCHAR", "VARCHAR"],
        "VARCHAR",
        type=duckdb.func.PythonUDFType.ARROW,
    )
