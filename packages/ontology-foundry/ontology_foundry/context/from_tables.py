from __future__ import annotations

import json
import math
from typing import Any, Literal

from ontology_foundry.analysis.stats import profile_categorical_column, profile_numeric_column, top_k_freq
from ontology_foundry.context.table_bundle import (
    ColumnContext,
    TabularContextBundle,
    column_context_from_profile,
)


def _freq_or_none(
    values: list[Any],
    *,
    profile_distinct: int | None,
    max_top_k: int,
) -> list[tuple[Any, int]] | None:
    if max_top_k <= 0:
        return None
    if profile_distinct is None or profile_distinct > max_top_k:
        return None
    return top_k_freq(values, k=max_top_k)


def _sample_rows_from_pandas(df: Any, *, max_sample_rows: int) -> list[dict[str, Any]]:
    import pandas as pd

    if max_sample_rows <= 0 or len(df) == 0:
        return []
    sub = df.head(max_sample_rows)
    blob = sub.to_json(orient="records", date_format="iso")
    if blob is None or blob == "":
        return []
    return json.loads(blob)


def _pandas_series_kind(s: Any) -> Literal["numeric", "categorical"]:
    from pandas.api.types import (
        is_bool_dtype,
        is_datetime64_any_dtype,
        is_numeric_dtype,
        is_timedelta64_dtype,
    )

    if is_bool_dtype(s.dtype):
        return "categorical"
    if is_numeric_dtype(s.dtype):
        return "numeric"
    if is_datetime64_any_dtype(s.dtype) or is_timedelta64_dtype(s.dtype):
        return "categorical"
    return "categorical"


def _pandas_series_to_numeric_values(s: Any) -> list[float | None]:
    import pandas as pd

    out: list[float | None] = []
    for v in s:
        if v is None or pd.isna(v):
            out.append(None)
        elif isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            out.append(None)
        else:
            out.append(float(v))
    return out


def _pandas_series_to_categorical_values(s: Any) -> list[Any]:
    import pandas as pd

    out: list[Any] = []
    for v in s:
        if v is None or pd.isna(v):
            out.append(None)
        else:
            out.append(v)
    return out


def bundle_from_pandas(
    df: Any,
    *,
    table_id: str,
    table_description: str | None = None,
    source_system: str | None = None,
    population_row_count: int | None = None,
    sample_description: str | None = None,
    max_top_k: int = 15,
    max_sample_rows: int = 80,
    extra_metadata: dict[str, str] | None = None,
    column_roles: dict[str, str] | None = None,
    stats_are_approximate: bool = False,
) -> TabularContextBundle:
    """
    Profile each column of a pandas ``DataFrame`` and build a :class:`TabularContextBundle`.

    Requires: ``pip install ontology-foundry[tabular]`` (pandas + pyarrow).

    * Numeric dtypes → ``profile_numeric_column``; if distinct count ≤ ``max_top_k``,
      top frequencies are attached (discrete numeric levels).
    * Other dtypes (string, bool, datetime, category, …) → ``profile_categorical_column``
      with ``top_k_freq`` up to ``max_top_k``.
    """
    try:
        import pandas as pd
    except ImportError as e:
        raise ImportError(
            "bundle_from_pandas requires pandas. Install: ontology-foundry[tabular]"
        ) from e

    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"bundle_from_pandas expected pandas.DataFrame, got {type(df)}")

    columns_ctx: list[ColumnContext] = []
    roles = column_roles or {}

    for col_key in list(df.columns):
        col_label = str(col_key)
        s = df[col_key]
        kind = _pandas_series_kind(s)
        declared_type = str(s.dtype)
        role = roles.get(col_label)

        if kind == "numeric":
            vals = _pandas_series_to_numeric_values(s)
            prof = profile_numeric_column(col_label, vals)
            freqs = _freq_or_none(vals, profile_distinct=prof.distinct_count, max_top_k=max_top_k)
            columns_ctx.append(
                column_context_from_profile(
                    col_label,
                    prof,
                    top_frequencies=freqs,
                    declared_type=declared_type,
                    role=role,
                    stats_are_approximate=stats_are_approximate,
                )
            )
        else:
            vals = _pandas_series_to_categorical_values(s)
            prof = profile_categorical_column(col_label, vals)
            freqs = top_k_freq(vals, k=max_top_k) if max_top_k > 0 else None
            columns_ctx.append(
                column_context_from_profile(
                    col_label,
                    prof,
                    top_frequencies=freqs,
                    declared_type=declared_type,
                    role=role,
                    stats_are_approximate=stats_are_approximate,
                )
            )

    sample_rows = _sample_rows_from_pandas(df, max_sample_rows=max_sample_rows)

    return TabularContextBundle(
        table_id=table_id,
        table_description=table_description,
        source_system=source_system,
        population_row_count=population_row_count,
        sample_description=sample_description,
        sample_rows=sample_rows,
        columns=columns_ctx,
        extra_metadata=dict(extra_metadata) if extra_metadata else {},
    )


def _arrow_type_kind(typ: Any) -> Literal["numeric", "categorical"]:
    import pyarrow.types as pat

    if pat.is_boolean(typ):
        return "categorical"
    if pat.is_integer(typ) or pat.is_floating(typ) or pat.is_decimal(typ):
        return "numeric"
    if pat.is_temporal(typ):
        return "categorical"
    return "categorical"


def _arrow_column_to_numeric_values(arr: Any) -> list[float | None]:
    raw: list[Any] = arr.to_pylist()
    out: list[float | None] = []
    for v in raw:
        if v is None:
            out.append(None)
        elif isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            out.append(None)
        else:
            try:
                out.append(float(v))  # int, Decimal, numpy scalar handled via float()
            except (TypeError, ValueError):
                out.append(None)
    return out


def _arrow_temporal_to_str(v: Any) -> Any:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _arrow_column_to_categorical_values(arr: Any, *, typ: Any) -> list[Any]:
    import pyarrow.types as pat

    raw = arr.to_pylist()
    if pat.is_temporal(typ):
        return [_arrow_temporal_to_str(v) for v in raw]
    return list(raw)


def _sample_rows_from_arrow(table: Any, *, max_sample_rows: int) -> list[dict[str, Any]]:
    if max_sample_rows <= 0 or table.num_rows == 0:
        return []
    n = min(max_sample_rows, table.num_rows)
    sub = table.slice(0, n)
    names = sub.column_names
    col_lists = [sub.column(name).to_pylist() for name in names]
    rows: list[dict[str, Any]] = []
    for i in range(sub.num_rows):
        rows.append({names[j]: col_lists[j][i] for j in range(len(names))})
    return json.loads(json.dumps(rows, default=str))


def bundle_from_arrow_table(
    table: Any,
    *,
    table_id: str,
    table_description: str | None = None,
    source_system: str | None = None,
    population_row_count: int | None = None,
    sample_description: str | None = None,
    max_top_k: int = 15,
    max_sample_rows: int = 80,
    extra_metadata: dict[str, str] | None = None,
    column_roles: dict[str, str] | None = None,
    stats_are_approximate: bool = False,
) -> TabularContextBundle:
    """
    Profile each column of a PyArrow ``Table`` and build a :class:`TabularContextBundle`.

    Requires ``pyarrow``. Install: ``ontology-foundry[tabular]`` (includes pyarrow), or
    ``pip install pyarrow`` on its own.
    columns use the categorical profiler (timestamps as ISO strings for frequency counts).
    """
    try:
        import pyarrow as pa
    except ImportError as e:
        raise ImportError(
            "bundle_from_arrow_table requires pyarrow. Install: ontology-foundry[tabular]"
        ) from e

    if not isinstance(table, pa.Table):
        raise TypeError(f"bundle_from_arrow_table expected pyarrow.Table, got {type(table)}")

    columns_ctx: list[ColumnContext] = []
    roles = column_roles or {}

    for i in range(table.num_columns):
        field = table.schema.field(i)
        field_name = field.name
        typ = field.type
        kind = _arrow_type_kind(typ)
        declared_type = str(typ)
        role = roles.get(field_name)

        arr = table.column(i)

        if kind == "numeric":
            vals = _arrow_column_to_numeric_values(arr)
            prof = profile_numeric_column(field_name, vals)
            freqs = _freq_or_none(vals, profile_distinct=prof.distinct_count, max_top_k=max_top_k)
            columns_ctx.append(
                column_context_from_profile(
                    field_name,
                    prof,
                    top_frequencies=freqs,
                    declared_type=declared_type,
                    role=role,
                    stats_are_approximate=stats_are_approximate,
                )
            )
        else:
            vals = _arrow_column_to_categorical_values(arr, typ=typ)
            prof = profile_categorical_column(field_name, vals)
            freqs = top_k_freq(vals, k=max_top_k) if max_top_k > 0 else None
            columns_ctx.append(
                column_context_from_profile(
                    field_name,
                    prof,
                    top_frequencies=freqs,
                    declared_type=declared_type,
                    role=role,
                    stats_are_approximate=stats_are_approximate,
                )
            )

    sample_rows = _sample_rows_from_arrow(table, max_sample_rows=max_sample_rows)

    return TabularContextBundle(
        table_id=table_id,
        table_description=table_description,
        source_system=source_system,
        population_row_count=population_row_count,
        sample_description=sample_description,
        sample_rows=sample_rows,
        columns=columns_ctx,
        extra_metadata=dict(extra_metadata) if extra_metadata else {},
    )
