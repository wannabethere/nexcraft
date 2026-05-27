"""ColumnStatDAO — write paths for foundry-derived tabular profiles.

Three-phase persistence to enforce PII gating:

  1. `upsert_aggregates(table_rk, column_aggregates, population_facts)` — runs
     IMMEDIATELY after introspect. Persists all scalar shape facts: n_rows,
     null_rate, distinct_count, mean/std/min/max, cardinality_tier. No values
     leave the runtime.

  2. `attach_sampled_values(table_rk, sample_rows, top_frequencies, gate)` —
     runs AFTER the `data_protection` enricher decides which columns are
     PII-safe. The `gate` callable returns True for a column_rk that may
     retain value-bearing fields. The DAO writes:
       - The full table-level `sample_rows` (already filtered by the caller
         to redact unsafe columns), and
       - Per-column `top_frequencies` (only for column_rks the gate cleared).
     `samples_persisted` flips True on the rows actually written.

  3. `clear_sampled_values(table_rk)` — operator escape hatch when policy
     changes; removes sample_rows + top_frequencies, leaves aggregates.

All three are idempotent on natural keys (`table_rk` / `column_rk`).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ontology_store.db.models import HierarchyAudit
from ontology_store.db.stats_models import CARDINALITY_TIERS, ColumnStat, TableStat

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# Input shapes — kept minimal so the pipeline doesn't import ORM models.
# ───────────────────────────────────────────────────────────────────────────


@dataclass
class ColumnAggregate:
    """Scalar shape facts for one column. Always safe to persist.

    Mirrors `ontology_foundry.analysis.models.NumericColumnProfile` plus a
    cardinality tier (foundry's `_resolve_cardinality` output).
    """
    column_rk: str
    table_rk: str
    n_rows: int
    null_rate: float
    distinct_count: int | None = None
    mean: float | None = None
    std: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    cardinality_tier: str | None = None  # low | medium | high | identifier
    declared_type: str | None = None
    role_hint: str | None = None
    stats_are_approximate: bool = False


@dataclass
class TableSampleFacts:
    """Table-level facts attached to a stats run. PII-gated."""
    table_rk: str
    population_row_count: int | None = None
    sample_row_count: int | None = None
    source_system: str | None = None
    sample_description: str | None = None
    sample_rows: list[dict[str, Any]] = field(default_factory=list)
    extra_metadata: dict[str, Any] = field(default_factory=dict)


# ───────────────────────────────────────────────────────────────────────────
# DAO
# ───────────────────────────────────────────────────────────────────────────


class ColumnStatDAO:
    """Caller manages the session. All methods are idempotent."""

    def __init__(self, session: Session, *, actor: str = "stats_profiler") -> None:
        self.s = session
        self.actor = actor

    # ── Phase 1: aggregates (always safe) ──────────────────────────────

    def upsert_aggregates(
        self,
        *,
        table_rk: str,
        aggregates: list[ColumnAggregate],
        population_row_count: int | None = None,
        source_system: str | None = None,
    ) -> dict[str, int]:
        """Upsert table-level + per-column aggregates. Returns counters.

        Touches `samples_persisted` only on insert (sets False) — re-running
        aggregates against a table whose samples were previously persisted
        leaves those samples alone. Use `attach_sampled_values` or
        `clear_sampled_values` to mutate sample state explicitly.
        """
        now = datetime.now(timezone.utc)
        # ── table_stat ───────────────────────────────────────────────────
        ts = self.s.get(TableStat, table_rk)
        if ts is None:
            ts = TableStat(
                table_rk=table_rk,
                population_row_count=population_row_count,
                source_system=source_system,
                samples_persisted=False,
            )
            self.s.add(ts)
        else:
            if population_row_count is not None:
                ts.population_row_count = population_row_count
            if source_system is not None:
                ts.source_system = source_system
            ts.updated_at = now

        # ── column_stat ──────────────────────────────────────────────────
        # Only attach stats to columns that exist in column_metadata; synthetic /
        # profiler-only columns (e.g. _last_touched_dt_utc), or a spine not yet
        # written, would violate column_stat_column_rk_fkey. Skip the unknowns.
        from sqlalchemy import select as _select
        from ontology_store.db.models import ColumnMetadata as _ColumnMetadata
        _rks = [a.column_rk for a in aggregates]
        _known = set(self.s.scalars(
            _select(_ColumnMetadata.rk).where(_ColumnMetadata.rk.in_(_rks))
        ).all()) if _rks else set()
        inserted = 0
        updated = 0
        skipped = 0
        for agg in aggregates:
            if agg.column_rk not in _known:
                skipped += 1
                continue
            if agg.cardinality_tier is not None and agg.cardinality_tier not in CARDINALITY_TIERS:
                raise ValueError(
                    f"Invalid cardinality_tier {agg.cardinality_tier!r}; "
                    f"expected one of {CARDINALITY_TIERS} or None"
                )
            row = self._get_by_column_rk(agg.column_rk)
            if row is None:
                self.s.add(ColumnStat(
                    column_rk=agg.column_rk,
                    table_rk=agg.table_rk,
                    n_rows=agg.n_rows,
                    null_rate=agg.null_rate,
                    distinct_count=agg.distinct_count,
                    mean=agg.mean,
                    std=agg.std,
                    min_value=agg.min_value,
                    max_value=agg.max_value,
                    cardinality_tier=agg.cardinality_tier,
                    declared_type=agg.declared_type,
                    role_hint=agg.role_hint,
                    stats_are_approximate=agg.stats_are_approximate,
                    samples_persisted=False,
                    top_frequencies=[],
                ))
                inserted += 1
            else:
                row.table_rk = agg.table_rk
                row.n_rows = agg.n_rows
                row.null_rate = agg.null_rate
                row.distinct_count = agg.distinct_count
                row.mean = agg.mean
                row.std = agg.std
                row.min_value = agg.min_value
                row.max_value = agg.max_value
                row.cardinality_tier = agg.cardinality_tier
                row.declared_type = agg.declared_type
                row.role_hint = agg.role_hint
                row.stats_are_approximate = agg.stats_are_approximate
                row.updated_at = now
                updated += 1
        if skipped:
            logger.debug(
                "upsert_aggregates: skipped %d column(s) with no column_metadata row (table_rk=%s)",
                skipped, table_rk,
            )
        self._audit(
            action="upsert_aggregates",
            entity_uid=f"table_stat:{table_rk}",
            new_value={"columns_inserted": inserted, "columns_updated": updated, "columns_skipped": skipped},
        )
        return {"inserted": inserted, "updated": updated, "skipped": skipped}

    # ── Phase 2: PII-gated sample values ───────────────────────────────

    def attach_sampled_values(
        self,
        *,
        table_facts: TableSampleFacts,
        column_top_frequencies: dict[str, list[dict[str, Any]]],
        gate: Callable[[str], bool],
    ) -> dict[str, int]:
        """Persist whole-row samples + per-column top-k after PII clearance.

        Args:
            table_facts: table-level sample to store. Caller should already
                have redacted unsafe column values from `sample_rows`.
            column_top_frequencies: per-column `column_rk → list[{value, count, share}]`.
                Rows whose column_rk the gate REJECTS are silently dropped.
            gate: `column_rk → bool`. True = column cleared for value retention.

        Returns counts: `{"columns_promoted": N, "columns_blocked": M, "row_sample_persisted": 0|1}`.
        """
        now = datetime.now(timezone.utc)
        ts = self.s.get(TableStat, table_facts.table_rk)
        if ts is None:
            raise ValueError(
                f"attach_sampled_values: table_stat row for {table_facts.table_rk!r} not found; "
                "call upsert_aggregates first."
            )
        ts.population_row_count = table_facts.population_row_count or ts.population_row_count
        ts.sample_row_count = table_facts.sample_row_count
        ts.source_system = table_facts.source_system or ts.source_system
        ts.sample_description = table_facts.sample_description
        ts.sample_rows = list(table_facts.sample_rows or [])
        ts.extra_metadata = dict(table_facts.extra_metadata or {})
        ts.samples_persisted = True
        ts.updated_at = now

        promoted = 0
        blocked = 0
        for col_rk, freqs in column_top_frequencies.items():
            if not gate(col_rk):
                blocked += 1
                continue
            row = self._get_by_column_rk(col_rk)
            if row is None:
                logger.debug(
                    "attach_sampled_values: skipping unknown column_rk=%s", col_rk,
                )
                continue
            row.top_frequencies = list(freqs or [])
            row.samples_persisted = True
            row.updated_at = now
            promoted += 1
        self._audit(
            action="attach_sampled_values",
            entity_uid=f"table_stat:{table_facts.table_rk}",
            new_value={
                "columns_promoted": promoted, "columns_blocked": blocked,
                "row_sample_persisted": 1 if table_facts.sample_rows else 0,
            },
        )
        return {
            "columns_promoted": promoted,
            "columns_blocked": blocked,
            "row_sample_persisted": 1 if table_facts.sample_rows else 0,
        }

    # ── Phase 3: clear samples (policy escape hatch) ────────────────────

    def clear_sampled_values(self, *, table_rk: str) -> int:
        """Remove sample_rows + top_frequencies for the table. Keeps aggregates."""
        now = datetime.now(timezone.utc)
        ts = self.s.get(TableStat, table_rk)
        cleared = 0
        if ts is not None:
            ts.sample_rows = []
            ts.samples_persisted = False
            ts.updated_at = now
            cleared += 1
        for row in self.s.execute(
            select(ColumnStat).where(ColumnStat.table_rk == table_rk),
        ).scalars():
            row.top_frequencies = []
            row.samples_persisted = False
            row.updated_at = now
            cleared += 1
        if cleared:
            self._audit(
                action="clear_sampled_values",
                entity_uid=f"table_stat:{table_rk}",
                new_value={"rows_cleared": cleared},
            )
        return cleared

    # ── Read paths ──────────────────────────────────────────────────────

    def get_table_stat(self, *, table_rk: str) -> TableStat | None:
        return self.s.get(TableStat, table_rk)

    def get_column_stat(self, *, column_rk: str) -> ColumnStat | None:
        return self._get_by_column_rk(column_rk)

    def list_column_stats(self, *, table_rk: str) -> list[ColumnStat]:
        return list(self.s.execute(
            select(ColumnStat).where(ColumnStat.table_rk == table_rk)
            .order_by(ColumnStat.column_rk.asc()),
        ).scalars().all())

    # ── Helpers ────────────────────────────────────────────────────────

    def _get_by_column_rk(self, column_rk: str) -> ColumnStat | None:
        return self.s.execute(
            select(ColumnStat).where(ColumnStat.column_rk == column_rk),
        ).scalar_one_or_none()

    def _audit(
        self,
        *,
        action: str,
        entity_uid: str,
        new_value: dict[str, Any] | None = None,
    ) -> None:
        self.s.add(HierarchyAudit(
            actor=self.actor,
            action=action,
            tier="column_stat",
            entity_uid=entity_uid,
            new_value=_json_safe(new_value) if new_value else None,
        ))


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value
