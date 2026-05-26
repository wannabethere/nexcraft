"""ORM models for foundry-derived tabular profiling.

Mirrors the two layers of `ontology_foundry.context.TabularContextBundle`:

  - `TableStat` — one row per `table_metadata.rk`. Holds whole-row samples
    (PII-gated) + population facts.
  - `ColumnStat` — one row per `column_metadata.rk`. Holds the per-column
    profile (`NumericColumnProfile` aggregates), top-k frequencies, and a
    cardinality tier classification.

Aggregates are written eagerly at introspect time. Samples and frequencies
flip on only after the `data_protection` enricher has had a chance to mark
a column sensitive — the `samples_persisted` boolean is the explicit gate
operators can audit.

These tables aren't authored — they're the persistence side of a runtime
extraction. Re-introspecting overwrites in place via the natural keys.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ontology_store.db.engine import Base


CARDINALITY_TIERS: tuple[str, ...] = ("low", "medium", "high", "identifier")


class TableStat(Base):
    """Table-level facts from a foundry profiling run.

    Natural key: `table_rk`. One row per table — re-profiling updates in place.

    Samples are PII-gated: `samples_persisted=False` means `sample_rows` is
    empty even though aggregates may already be populated. Operators can
    promote a table's sample to persisted-status after policy review.
    """
    __tablename__ = "table_stat"

    table_rk: Mapped[str] = mapped_column(
        ForeignKey("table_metadata.rk", ondelete="CASCADE"),
        primary_key=True,
    )
    population_row_count: Mapped[int | None] = mapped_column(BigInteger)
    sample_row_count: Mapped[int | None] = mapped_column(Integer)
    source_system: Mapped[str | None] = mapped_column(Text)
    sample_description: Mapped[str | None] = mapped_column(Text)
    sample_rows: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list,
    )
    samples_persisted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict,
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class ColumnStat(Base):
    """Per-column profile from a foundry profiling run.

    Natural key: `column_rk` (`UniqueConstraint`). `table_rk` is denormalised
    onto the row so cardinality-tier scans don't need a join.

    Always populated immediately at introspect time:
      n_rows, null_rate, distinct_count, mean, std, min_value, max_value,
      cardinality_tier, declared_type.

    PII-gated (written only after `samples_persisted=True`):
      top_frequencies. Holding the top-k under the same gate as the row
      sample lets us treat all value-bearing fields uniformly.
    """
    __tablename__ = "column_stat"

    column_stat_pk: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    column_rk: Mapped[str] = mapped_column(
        ForeignKey("column_metadata.rk", ondelete="CASCADE"), nullable=False,
    )
    table_rk: Mapped[str] = mapped_column(Text, nullable=False)

    # Aggregates — scalar shape facts. Always written.
    n_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    null_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    distinct_count: Mapped[int | None] = mapped_column(Integer)
    mean: Mapped[float | None] = mapped_column(Float)
    std: Mapped[float | None] = mapped_column(Float)
    min_value: Mapped[float | None] = mapped_column(Float)
    max_value: Mapped[float | None] = mapped_column(Float)

    # Derived classification — low/medium/high/identifier (from foundry's
    # `_resolve_cardinality`). Useful for retrieval filtering.
    cardinality_tier: Mapped[str | None] = mapped_column(Text)

    # PII-gated value-bearing fields.
    top_frequencies: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list,
    )
    samples_persisted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )

    declared_type: Mapped[str | None] = mapped_column(Text)
    role_hint: Mapped[str | None] = mapped_column(Text)
    stats_are_approximate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("column_rk", name="uq_column_stat_column_rk"),
        CheckConstraint(
            "cardinality_tier IS NULL OR "
            "cardinality_tier IN ('low','medium','high','identifier')",
            name="ck_column_stat_cardinality_tier",
        ),
    )
