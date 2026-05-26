from __future__ import annotations

import json
from typing import Any, Literal

from ontology_foundry.context.table_bundle import TabularContextBundle, render_tabular_context

ContextFormat = Literal["markdown", "json"]


def render_json_context(bundle: TabularContextBundle, *, max_sample_rows: int = 5) -> str:
    """Structured JSON arm — same facts as markdown, no prose blocks."""
    columns_out: list[dict[str, Any]] = []
    for col in bundle.columns:
        st = col.stats
        tier = col.cardinality_hint
        if tier == "auto":
            tier = "unknown"
        columns_out.append(
            {
                "name": col.name,
                "declared_type": col.declared_type,
                "role": col.role,
                "cardinality_tier": tier,
                "n_rows": st.n_rows if st else None,
                "null_rate": st.null_rate if st else None,
                "distinct_count": st.distinct_count if st else None,
                "mean": st.mean if st else None,
                "std": st.std if st else None,
                "min": st.min if st else None,
                "max": st.max if st else None,
            }
        )
    payload = {
        "table_id": bundle.table_id,
        "table_description": bundle.table_description,
        "source_system": bundle.source_system,
        "population_row_count": bundle.population_row_count,
        "sample_description": bundle.sample_description,
        "columns": columns_out,
        "sample_rows": bundle.sample_rows[: max(0, max_sample_rows)],
    }
    return json.dumps(payload, indent=2, default=str)


def render_context(
    bundle: TabularContextBundle,
    fmt: ContextFormat,
    *,
    max_sample_rows: int = 5,
) -> str:
    if fmt == "markdown":
        return render_tabular_context(bundle, max_sample_rows=max_sample_rows)
    if fmt == "json":
        return render_json_context(bundle, max_sample_rows=max_sample_rows)
    raise ValueError(f"unknown format: {fmt}")
