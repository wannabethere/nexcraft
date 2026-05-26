"""Add table_stat + column_stat — persistence layer for foundry profiling output.

`bundle_from_pandas` produces a `TabularContextBundle` that has two layers:

  - Table-level: population_row_count, sample_rows (list[dict]), source_system.
  - Column-level: stats (NumericColumnProfile), top_frequencies, declared_type,
    cardinality_hint.

We persist both layers to keep grounding cheap on subsequent runs:

  - `table_stat`: one row per table_rk. Holds the row sample (PII-gated) and
    population facts.
  - `column_stat`: one row per column_rk. Holds the per-column profile,
    top-k frequencies, cardinality tier, and the per-column slice of the
    sample (gated independently — a PII column's stats are still safe to
    store, only its sample values get suppressed).

Sample retention is PII-aware: `samples_persisted` flips true only after the
`data_protection` enricher has had a chance to mark a column as sensitive.
Aggregates (n_rows, null_rate, distinct_count, min/max/mean/stddev) are
always written immediately at introspect time — they're scalar facts about
shape, not data values.

Revision ID: 0007_add_column_stats
Revises: 0006_add_card_storage
Create Date: 2026-05-17
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0007_add_column_stats"
down_revision: Union[str, None] = "0006_add_card_storage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "table_stat",
        sa.Column("table_rk", sa.Text(), primary_key=True),
        sa.Column("population_row_count", sa.BigInteger(), nullable=True),
        sa.Column("sample_row_count", sa.Integer(), nullable=True),
        sa.Column("source_system", sa.Text(), nullable=True),
        sa.Column("sample_description", sa.Text(), nullable=True),
        # Whole-row sample, PII-gated. Empty list when sensitivity unclear.
        sa.Column("sample_rows", JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "samples_persisted", sa.Boolean(), nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "extra_metadata", JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["table_rk"], ["table_metadata.rk"], ondelete="CASCADE",
            name="fk_table_stat_table",
        ),
    )

    op.create_table(
        "column_stat",
        sa.Column("column_stat_pk", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("column_rk", sa.Text(), nullable=False),
        sa.Column("table_rk", sa.Text(), nullable=False),
        # Aggregates (the always-safe scalar facts).
        sa.Column("n_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("null_rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("distinct_count", sa.Integer(), nullable=True),
        sa.Column("mean", sa.Float(), nullable=True),
        sa.Column("std", sa.Float(), nullable=True),
        sa.Column("min_value", sa.Float(), nullable=True),
        sa.Column("max_value", sa.Float(), nullable=True),
        # Derived classification — low/medium/high/identifier.
        sa.Column("cardinality_tier", sa.Text(), nullable=True),
        # Top-k frequencies — PII-gated; written only when samples_persisted.
        sa.Column(
            "top_frequencies", JSONB(), nullable=False, server_default="[]",
        ),
        sa.Column(
            "samples_persisted", sa.Boolean(), nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("declared_type", sa.Text(), nullable=True),
        sa.Column("role_hint", sa.Text(), nullable=True),
        sa.Column(
            "stats_are_approximate", sa.Boolean(), nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("column_rk", name="uq_column_stat_column_rk"),
        sa.ForeignKeyConstraint(
            ["column_rk"], ["column_metadata.rk"], ondelete="CASCADE",
            name="fk_column_stat_column",
        ),
        sa.CheckConstraint(
            "cardinality_tier IS NULL OR "
            "cardinality_tier IN ('low','medium','high','identifier')",
            name="ck_column_stat_cardinality_tier",
        ),
    )
    op.create_index("idx_column_stat_table", "column_stat", ["table_rk"])
    op.create_index(
        "idx_column_stat_cardinality",
        "column_stat",
        ["cardinality_tier"],
        postgresql_where=sa.text("cardinality_tier IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_column_stat_cardinality", table_name="column_stat")
    op.drop_index("idx_column_stat_table", table_name="column_stat")
    op.drop_table("column_stat")
    op.drop_table("table_stat")
