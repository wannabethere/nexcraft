"""FedSQL query activity — Phase J.0."""
from __future__ import annotations

import math
import time
import uuid
from datetime import date, datetime, time as _time
from decimal import Decimal
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from nexcraft.core.context import QueryContext

from nexcraft_jobs.runtime.worker_config import get_worker_fedsql
from nexcraft_jobs.schemas import ColumnSpec, ExecutionResult, FedSQLQueryInput


# Postgres SQLSTATE classes that are DETERMINISTIC — the same statement will fail
# identically on every retry, so retrying only wastes time (and can blow the
# caller's submit-wait timeout, masking the real error). 42 = syntax error or
# access rule violation (undefined column/function/table, datatype mismatch);
# 22 = data exception (bad cast, invalid input). Everything else (08 connection,
# 40 deadlock/serialization, 53/57/58 resource/operator/system) stays retryable.
#   42 = syntax error / access rule violation (undefined col/func/table, bad GROUP BY)
#   22 = data exception (bad cast, invalid input)
#   0A = feature not supported (e.g. ORDER BY <expression> after UNION — 0A000)
#   54 = program limit exceeded (too many columns / args / nesting)
_DETERMINISTIC_SQLSTATE_CLASSES = ("42", "22", "0A", "54")
_DETERMINISTIC_SQL_PHRASES = (
    "operator does not exist",
    "does not exist",
    "syntax error",
    "datatype mismatch",
    "cannot be cast",
    "invalid input syntax",
    "undefined",
    "ambiguous",
    "only result column names",   # UNION ORDER BY by expression (0A000)
    "must appear in the group by",
)


def _is_deterministic_sql_error(exc: BaseException) -> bool:
    """True for bad-SQL errors (type/column/syntax) that can't succeed on retry."""
    state = getattr(exc, "sqlstate", None)
    if isinstance(state, str) and state[:2] in _DETERMINISTIC_SQLSTATE_CLASSES:
        return True
    msg = str(exc).lower()
    return any(p in msg for p in _DETERMINISTIC_SQL_PHRASES)


def _arrow_type_name(field) -> str:
    try:
        return str(field.type)
    except Exception:
        return "unknown"


def _json_safe(value: Any) -> Any:
    """Coerce a result value into a JSON-serializable primitive.

    Postgres/asyncpg → pyarrow ``to_pylist()`` hands back Python ``Decimal``,
    ``date``/``datetime``, ``uuid.UUID``, ``bytes`` and NaN/Inf floats — none of
    which Temporal's default JSON payload converter can encode (it raises
    ``TypeError: Object of type X is not JSON serializable``). Normalize every
    cell so ANY query's rows round-trip through Temporal, regardless of column
    types (DECIMAL, dates, UUIDs, …).
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None  # NaN / Inf → null
    if isinstance(value, Decimal):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, (datetime, date, _time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        b = bytes(value)
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return b.hex()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "isoformat"):  # any other date/time-like
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)  # uuid.UUID and any other object → string


async def _execute_fedsql(params: FedSQLQueryInput) -> ExecutionResult:
    client = get_worker_fedsql()
    source_id = params.resolved_source_id()
    query_id = params.query_id or f"fedsql-{uuid.uuid4().hex[:12]}"
    qc = QueryContext(
        tenant_id=params.tenant_id,
        query_id=query_id,
        trace_id=params.trace_id,
        max_rows=params.row_limit,
    )
    t0 = time.perf_counter()
    try:
        table = await client.execute_to_table(source_id, params.sql, qc)
    except Exception as exc:
        if _is_deterministic_sql_error(exc):
            # Bad SQL — retrying the identical statement can't help. Fail fast and
            # non-retryable so the CLEAN DB error reaches the caller's self-correct
            # loop immediately (instead of being masked by retries / submit timeout).
            raise ApplicationError(
                f"SQL execution failed: {exc}",
                type=type(exc).__name__,
                non_retryable=True,
            ) from exc
        raise  # transient/infra error — let Temporal's retry policy handle it
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    if table.num_rows > params.row_limit:
        table = table.slice(0, params.row_limit)

    columns = [
        ColumnSpec(name=f.name, type=_arrow_type_name(f)) for f in table.schema
    ]
    return ExecutionResult(
        rows=[_json_safe(row) for row in table.to_pylist()],
        columns=columns,
        row_count=table.num_rows,
        exec_time_ms=elapsed_ms,
        warnings=[],
        errors=[],
        success=True,
    )


@activity.defn(name="fedsql_execute_to_dataframe")
async def fedsql_execute_to_dataframe(
    params: FedSQLQueryInput | dict[str, Any],
) -> ExecutionResult:
    """Run SQL via worker-configured FedSQLClient; return structured rows."""
    payload = (
        params
        if isinstance(params, FedSQLQueryInput)
        else FedSQLQueryInput.model_validate(params)
    )
    return await _execute_fedsql(payload)
