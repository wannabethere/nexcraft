"""TableProfiler — pull a sample from the source, call foundry, return a bundle.

The profiler is a thin glue layer between three things:

  - The source (Postgres v1, future Snowflake/Salesforce/…). We use psycopg to
    issue `SELECT * FROM schema.table LIMIT n` against a DSN looked up by the
    same source_id-aware machinery the validator uses.
  - Foundry's `bundle_from_pandas`, which does the actual profiling work.
  - `ColumnStatDAO`, which persists what comes back.

Test stubs:
  - `TableProfiler(sample_loader=...)` accepts a callable
    `(source_id, schema, table, limit) -> pd.DataFrame`, so unit tests can
    inject canned data without standing up a real database.

Bundle conversion helpers (`bundle_to_*`) are pure: they convert a foundry
`TabularContextBundle` into the row shapes the DAO expects. They're separate
from the network-bound `TableProfiler.profile()` so the DAO write path is
easy to test independently.
"""
from __future__ import annotations

import logging
import re
import re as _re  # noqa: F401  — kept stable for potential extension
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal

from ontology_store.dao.stats import ColumnAggregate, TableSampleFacts

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd
    from ontology_foundry.context.table_bundle import (
        ColumnContext,
        TabularContextBundle,
    )

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# TableProfiler — runtime entry point
# ───────────────────────────────────────────────────────────────────────────


# A loader pulls a sample DataFrame for one table. Default impl uses psycopg;
# tests inject a stub returning a canned DataFrame.
SampleLoader = Callable[[str, str, str, int], "pd.DataFrame"]


@dataclass
class TableProfiler:
    """Build a foundry `TabularContextBundle` for one MDL asset.

    Args:
        sample_loader: callable `(source_id, schema, table, limit)` returning
            a pandas DataFrame. Defaults to `psycopg_sample_loader(dsn_for=...)`
            when `dsn_for` is provided.
        dsn_for: callable `source_id → DSN` for the default psycopg loader.
            Required when `sample_loader` is not supplied.
        sample_limit: row cap on the SELECT (default 1000). Bigger sample
            tightens null_rate / distinct_count accuracy at linear cost.
        max_top_k: per-column top-k frequency cap (default 15). Forwarded to
            `bundle_from_pandas`.
        max_sample_rows: cap on whole-row sample retention (default 80).
    """
    sample_loader: SampleLoader | None = None
    dsn_for: Callable[[str], str] | None = None
    sample_limit: int = 1000
    max_top_k: int = 15
    max_sample_rows: int = 80

    def __post_init__(self) -> None:
        if self.sample_loader is None and self.dsn_for is not None:
            self.sample_loader = _psycopg_sample_loader(self.dsn_for)
        if self.sample_loader is None:
            raise ValueError(
                "TableProfiler requires either sample_loader= or dsn_for= "
                "to be supplied."
            )

    def profile(
        self,
        *,
        source_id: str,
        schema: str,
        table: str,
        table_id: str,
        table_description: str | None = None,
        population_row_count: int | None = None,
    ) -> "TabularContextBundle | None":
        """Pull a sample and call foundry. Returns None on sampler error.

        Per-call exceptions are caught and logged — a single bad table never
        blocks the introspect pass.
        """
        try:
            from ontology_foundry.context.from_tables import bundle_from_pandas
        except ImportError as exc:
            logger.warning(
                "TableProfiler: foundry context unavailable (%s); skipping profile",
                exc,
            )
            return None

        loader = self.sample_loader
        assert loader is not None  # post_init guarantees this
        try:
            df = loader(source_id, schema, table, self.sample_limit)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "TableProfiler: sample loader failed for %s.%s on %s: %s",
                schema, table, source_id, exc,
            )
            return None
        if df is None or df.empty:
            logger.info(
                "TableProfiler: no rows sampled from %s.%s on %s", schema, table, source_id,
            )
            return None

        try:
            return bundle_from_pandas(
                df,
                table_id=table_id,
                table_description=table_description,
                source_system=f"postgres:{source_id}",
                population_row_count=population_row_count,
                sample_description=f"random LIMIT {self.sample_limit}",
                max_top_k=self.max_top_k,
                max_sample_rows=self.max_sample_rows,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "TableProfiler: bundle_from_pandas failed for %s.%s: %s",
                schema, table, exc,
            )
            return None


# ───────────────────────────────────────────────────────────────────────────
# Bundle → DAO shape conversion (pure)
# ───────────────────────────────────────────────────────────────────────────


def bundle_to_aggregates(
    *,
    table_rk: str,
    bundle: "TabularContextBundle",
    column_rk_by_name: dict[str, str],
    role_hints: dict[str, str] | None = None,
) -> list[ColumnAggregate]:
    """Project a foundry bundle into ColumnAggregate rows.

    Skips columns missing from `column_rk_by_name` — the MDL is authoritative
    for which columns exist; bundle columns that no longer have an MDL entry
    have been dropped or renamed and shouldn't be persisted.
    """
    role_hints = role_hints or {}
    aggregates: list[ColumnAggregate] = []
    for col in bundle.columns:
        col_rk = column_rk_by_name.get(col.name)
        if col_rk is None:
            logger.debug(
                "bundle_to_aggregates: bundle column %s not in MDL — skipping", col.name,
            )
            continue
        stats = col.stats
        aggregates.append(ColumnAggregate(
            column_rk=col_rk,
            table_rk=table_rk,
            n_rows=int(stats.n_rows) if stats and stats.n_rows is not None else 0,
            null_rate=float(stats.null_rate) if stats and stats.null_rate is not None else 0.0,
            distinct_count=int(stats.distinct_count) if stats and stats.distinct_count is not None else None,
            mean=_safe_float(stats.mean) if stats else None,
            std=_safe_float(stats.std) if stats else None,
            min_value=_safe_float(stats.min) if stats else None,
            max_value=_safe_float(stats.max) if stats else None,
            cardinality_tier=resolve_cardinality_tier(col),
            declared_type=col.declared_type,
            role_hint=role_hints.get(col.name) or col.role,
            stats_are_approximate=bool(col.stats_are_approximate),
        ))
    return aggregates


def bundle_to_table_facts(
    *,
    table_rk: str,
    bundle: "TabularContextBundle",
) -> TableSampleFacts:
    """Project the table-level layer of the bundle into a DAO input."""
    return TableSampleFacts(
        table_rk=table_rk,
        population_row_count=bundle.population_row_count,
        sample_row_count=len(bundle.sample_rows) or None,
        source_system=bundle.source_system,
        sample_description=bundle.sample_description,
        sample_rows=list(bundle.sample_rows or []),
        extra_metadata=dict(bundle.extra_metadata or {}),
    )


def bundle_to_top_frequencies(
    *,
    bundle: "TabularContextBundle",
    column_rk_by_name: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    """Per-column `column_rk → [{value, count, share}, …]` map.

    Format matches the JSONB shape in `column_stat.top_frequencies` so the
    caller can hand it straight to `attach_sampled_values`.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for col in bundle.columns:
        col_rk = column_rk_by_name.get(col.name)
        if col_rk is None:
            continue
        out[col_rk] = [
            {"value": f.value, "count": f.count, "share": f.share}
            for f in col.top_frequencies
        ]
    return out


# ───────────────────────────────────────────────────────────────────────────
# Cardinality tier — mirrors foundry's `_resolve_cardinality` (private there).
# Kept here so we don't import a private name.
# ───────────────────────────────────────────────────────────────────────────


_ID_NAME = re.compile(r"(^id$|_id$|uuid|uid$|^pk$|^sk$|_guid$)", re.IGNORECASE)
_Tier = Literal["low", "medium", "high", "identifier"]


def resolve_cardinality_tier(col: "ColumnContext") -> str | None:
    """Bucket a column's cardinality. Returns None when stats are missing."""
    hint = getattr(col, "cardinality_hint", "auto")
    if hint != "auto":
        return hint
    stats = col.stats
    if not stats or stats.distinct_count is None or stats.n_rows is None:
        return None
    non_null = max(0, int(round((1.0 - float(stats.null_rate or 0)) * int(stats.n_rows))))
    if non_null == 0:
        return "low"
    d = int(stats.distinct_count)
    ratio = d / non_null
    name_hints_id = bool(_ID_NAME.search(col.name))
    if ratio >= 0.97 or (name_hints_id and ratio >= 0.90 and d >= 10):
        return "identifier"
    if d <= 20 or ratio <= 0.03:
        return "low"
    if ratio >= 0.55:
        return "high"
    return "medium"


# ───────────────────────────────────────────────────────────────────────────
# Default sample loader (psycopg)
# ───────────────────────────────────────────────────────────────────────────


def _psycopg_sample_loader(
    dsn_for: Callable[[str], str],
) -> SampleLoader:
    """Build a sample loader that issues `SELECT * FROM s.t LIMIT n` via psycopg."""

    def _load(source_id: str, schema: str, table: str, limit: int) -> "pd.DataFrame":
        import pandas as pd
        import psycopg
        from psycopg import sql as psql

        query = psql.SQL("SELECT * FROM {schema}.{table} LIMIT {limit}").format(
            schema=psql.Identifier(schema),
            table=psql.Identifier(table),
            limit=psql.Literal(limit),
        )
        dsn = dsn_for(source_id)
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                cols = [d.name for d in (cur.description or [])]
                rows = cur.fetchall()
        return pd.DataFrame(rows, columns=cols)

    return _load


# ───────────────────────────────────────────────────────────────────────────
# Misc helpers
# ───────────────────────────────────────────────────────────────────────────


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # NaN/Inf are not safe for JSON or DB columns
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f
