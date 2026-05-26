from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ontology_foundry.analysis.models import NumericColumnProfile
from ontology_foundry.context.table_bundle import ColumnContext, TabularContextBundle


def _column_name_from_rk(column_rk: str) -> str:
    return column_rk.rsplit("/", 1)[-1]


def bundle_from_preview_files(
    aggregates_path: Path,
    samples_path: Path,
    *,
    max_sample_rows: int = 5,
) -> TabularContextBundle:
    """Rebuild a TabularContextBundle from ontology-pipeline preview artifacts."""
    agg = json.loads(aggregates_path.read_text(encoding="utf-8"))
    samples_payload = json.loads(samples_path.read_text(encoding="utf-8"))

    table_rk = agg.get("table_rk") or samples_payload.get("table_rk") or "unknown_table"
    table_name = _column_name_from_rk(table_rk) if "/" in table_rk else table_rk

    columns: list[ColumnContext] = []
    for row in agg.get("columns") or []:
        name = _column_name_from_rk(row["column_rk"])
        tier = row.get("cardinality_tier")
        hint = tier if tier in ("low", "medium", "high", "identifier") else "auto"
        columns.append(
            ColumnContext(
                name=name,
                declared_type=row.get("declared_type"),
                role=row.get("role_hint"),
                stats=NumericColumnProfile(
                    column=name,
                    n_rows=int(row.get("n_rows") or 0),
                    null_rate=float(row.get("null_rate") or 0.0),
                    distinct_count=row.get("distinct_count"),
                    mean=row.get("mean"),
                    std=row.get("std"),
                    min=row.get("min_value"),
                    max=row.get("max_value"),
                ),
                top_frequencies=[],
                cardinality_hint=hint,  # type: ignore[arg-type]
                stats_are_approximate=bool(row.get("stats_are_approximate")),
            )
        )

    sample_rows: list[dict[str, Any]] = list(samples_payload.get("sample_rows") or [])
    if max_sample_rows > 0:
        sample_rows = sample_rows[:max_sample_rows]

    return TabularContextBundle(
        table_id=table_rk,
        table_description=f"Preview table {table_name}",
        source_system=agg.get("source_system"),
        population_row_count=agg.get("population_row_count"),
        sample_description=f"preview sample (cap {max_sample_rows} rows in context)",
        sample_rows=sample_rows,
        columns=columns,
        extra_metadata={"preview_table": table_name},
    )


def default_preview_paths(preview_dir: Path) -> tuple[Path, Path]:
    base = preview_dir / "column_stats" / "csod-local" / "public"
    return (
        base / "users_core.aggregates.json",
        base / "users_core.samples.json",
    )
