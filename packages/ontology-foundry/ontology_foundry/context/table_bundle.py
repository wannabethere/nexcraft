from __future__ import annotations

import json
import math
import re
from typing import TYPE_CHECKING, Any, Literal, Sequence

from pydantic import BaseModel, Field

from ontology_foundry.analysis.models import NumericColumnProfile

if TYPE_CHECKING:
    from ontology_foundry.models import Document


class FrequencyEntry(BaseModel):
    """Single (value, count) pair; value is stringified for LLM-safe display."""

    value: str
    count: int
    share: float | None = Field(
        default=None,
        description="Optional precomputed share of non-null rows (0-1).",
    )


class ColumnContext(BaseModel):
    """
    One column’s statistical footprint plus optional frequencies for categoricals.
    Feed :class:`NumericColumnProfile` from ``profile_numeric_column`` /
    ``profile_categorical_column``.
    """

    name: str
    declared_type: str | None = None
    role: str | None = Field(
        default=None,
        description='Optional hint, e.g. "primary_key", "measure", "dimension".',
    )
    stats: NumericColumnProfile | None = None
    top_frequencies: list[FrequencyEntry] = Field(default_factory=list)
    cardinality_hint: Literal["auto", "low", "medium", "high", "identifier"] = "auto"
    stats_are_approximate: bool = False


class TabularContextBundle(BaseModel):
    """
    Everything needed to build grounded natural-language context for an LLM:
    table identity, optional population facts, per-column stats, and row sample.
    """

    table_id: str
    table_description: str | None = None
    source_system: str | None = Field(
        default=None,
        description='e.g. "postgres:hr.employees", "salesforce:Contact".',
    )
    population_row_count: int | None = Field(
        default=None,
        description="Full table row count when known (sample may be smaller).",
    )
    sample_description: str | None = Field(
        default=None,
        description='Human-readable how the sample was taken, e.g. "random n=500".',
    )
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)
    columns: list[ColumnContext] = Field(default_factory=list)
    extra_metadata: dict[str, str] = Field(default_factory=dict)


_ID_NAME = re.compile(
    r"(^id$|_id$|uuid|uid$|^pk$|^sk$|_guid$)",
    re.IGNORECASE,
)


def column_context_from_profile(
    name: str,
    profile: NumericColumnProfile,
    *,
    top_frequencies: Sequence[tuple[Any, int]] | None = None,
    declared_type: str | None = None,
    role: str | None = None,
    cardinality_hint: Literal["auto", "low", "medium", "high", "identifier"] = "auto",
    stats_are_approximate: bool = False,
) -> ColumnContext:
    """Helper: wrap an existing :class:`NumericColumnProfile` into :class:`ColumnContext`."""
    freqs: list[FrequencyEntry] = []
    non_null = _non_null_count(profile)
    for v, c in top_frequencies or []:
        share = (float(c) / non_null) if non_null > 0 else None
        freqs.append(FrequencyEntry(value=_stringify_value(v), count=int(c), share=share))
    return ColumnContext(
        name=name,
        declared_type=declared_type,
        role=role,
        stats=profile,
        top_frequencies=freqs,
        cardinality_hint=cardinality_hint,
        stats_are_approximate=stats_are_approximate,
    )


def _stringify_value(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return str(v)
    return str(v)


def _non_null_count(p: NumericColumnProfile) -> int:
    return max(0, int(round((1.0 - p.null_rate) * p.n_rows)))


def _resolve_cardinality(col: ColumnContext) -> Literal["low", "medium", "high", "identifier"]:
    if col.cardinality_hint != "auto":
        return col.cardinality_hint  # type: ignore[return-value]
    st = col.stats
    if not st or st.distinct_count is None:
        return "medium"
    non_null = _non_null_count(st)
    if non_null == 0:
        return "low"
    d = st.distinct_count
    ratio = d / non_null
    name_hints_id = bool(_ID_NAME.search(col.name))
    if ratio >= 0.97 or (name_hints_id and ratio >= 0.90 and d >= 10):
        return "identifier"
    if d <= 20 or ratio <= 0.03:
        return "low"
    if ratio >= 0.55:
        return "high"
    return "medium"


def _fmt_pct(x: float) -> str:
    return f"{100.0 * x:.2f}%"


def _render_column_block(col: ColumnContext) -> list[str]:
    lines: list[str] = []
    lines.append(f"### Column `{col.name}`")
    tier = _resolve_cardinality(col)
    approx = " (approximate)" if col.stats_are_approximate else ""
    lines.append(f"- Cardinality class: **{tier}**{approx}.")
    if col.declared_type:
        lines.append(f"- Declared type: `{col.declared_type}`.")
    if col.role:
        lines.append(f"- Role hint: {col.role}.")

    st = col.stats
    if st:
        lines.append(
            f"- Profiled on **{st.n_rows}** row(s); null rate: **{_fmt_pct(st.null_rate)}**."
        )
        if st.distinct_count is not None:
            lines.append(f"- Distinct values: **{st.distinct_count}**.")
        if tier == "identifier":
            lines.append(
                "- Interpretation: values are nearly unique per row (identifier-like). "
                "Use as keys or filters; do not treat as a small categorical enum."
            )
        elif tier == "high":
            lines.append(
                "- Interpretation: many distinct values — summarize with ranges or samples, "
                "not exhaustive level lists."
            )
        elif tier == "low":
            lines.append(
                "- Interpretation: few distinct values — distribution can be described "
                "explicitly when frequencies are provided."
            )
        else:
            lines.append(
                "- Interpretation: medium cardinality — use top frequencies plus distinct count."
            )

        numeric_bits: list[str] = []
        if st.min is not None and st.max is not None:
            numeric_bits.append(f"min={st.min:g}, max={st.max:g}")
        if st.mean is not None:
            numeric_bits.append(f"mean={st.mean:g}")
        if st.std is not None:
            numeric_bits.append(f"std={st.std:g}")
        if numeric_bits:
            lines.append(f"- Numeric summary: {', '.join(numeric_bits)}.")

    if col.top_frequencies and tier in ("low", "medium", "high"):
        non_null = _non_null_count(st) if st else 0
        parts: list[str] = []
        for ent in col.top_frequencies:
            share = ent.share
            if share is None and non_null > 0:
                share = ent.count / non_null
            if share is not None:
                parts.append(f"`{ent.value}`: {ent.count} ({_fmt_pct(share)})")
            else:
                parts.append(f"`{ent.value}`: {ent.count}")
        lines.append("- Top frequencies: " + "; ".join(parts) + ".")
        if st and st.distinct_count is not None and len(col.top_frequencies) < st.distinct_count:
            lines.append(
                f"- Note: only the top {len(col.top_frequencies)} values are listed "
                f"({st.distinct_count} distinct overall)."
            )

    lines.append("")
    return lines


def render_tabular_context(
    bundle: TabularContextBundle,
    *,
    max_sample_rows: int = 80,
    json_indent: int | None = 2,
) -> str:
    """
    Render a deterministic Markdown-ish string for LLM context.

    Numbers come only from ``bundle`` / embedded profiles — no model calls.
    """
    parts: list[str] = []
    parts.append("## Tabular context (statistics + sample)")
    parts.append("")
    parts.append(
        "The following describes a table using **caller-supplied** statistics and a "
        "**row sample**. Counts, rates, and summaries are factual only as provided; "
        "do not infer extra columns or values."
    )
    parts.append("")
    parts.append(f"- **Table id:** `{bundle.table_id}`")
    if bundle.table_description:
        parts.append(f"- **Description:** {bundle.table_description}")
    if bundle.source_system:
        parts.append(f"- **Source:** {bundle.source_system}")
    if bundle.population_row_count is not None:
        parts.append(f"- **Population row count (reported):** {bundle.population_row_count}")
    if bundle.sample_description:
        parts.append(f"- **Sample:** {bundle.sample_description}")
    parts.append("")

    if bundle.extra_metadata:
        parts.append("### Additional metadata")
        for k, v in sorted(bundle.extra_metadata.items()):
            parts.append(f"- `{k}`: {v}")
        parts.append("")

    if bundle.columns:
        parts.append("## Column profiles")
        parts.append("")
        for col in bundle.columns:
            parts.extend(_render_column_block(col))

    if bundle.sample_rows:
        trimmed = bundle.sample_rows[: max(0, max_sample_rows)]
        parts.append("## Sample rows (JSON)")
        parts.append("")
        parts.append("```json")
        parts.append(
            json.dumps(trimmed, indent=json_indent, default=str, sort_keys=False),
        )
        parts.append("```")
        if len(bundle.sample_rows) > len(trimmed):
            parts.append("")
            parts.append(
                f"_({len(bundle.sample_rows) - len(trimmed)} more row(s) omitted; "
                f"cap={max_sample_rows}.)_"
            )

    return "\n".join(parts).rstrip() + "\n"


def tabular_context_as_document(
    bundle: TabularContextBundle,
    *,
    doc_id: str = "tabular-context",
    max_sample_rows: int = 80,
    extra_metadata: dict[str, str] | None = None,
) -> Document:
    """
    Render ``bundle`` and wrap as :class:`~ontology_foundry.models.Document`
    for NER / retrieval / relation pipelines.
    """
    from ontology_foundry.models import Document

    body = render_tabular_context(bundle, max_sample_rows=max_sample_rows)
    meta: dict[str, str] = {"kind": "tabular_context", "table_id": bundle.table_id}
    if bundle.source_system:
        meta["source_system"] = bundle.source_system
    if extra_metadata:
        meta.update(extra_metadata)
    return Document(doc_id=doc_id, text=body, metadata=meta)
