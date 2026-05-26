"""Add relation_type (TBox) + lineage_edge.predicate_id.

Pairs with `ontology_foundry.relations.induce_schema` which produces a
canonicalized predicate vocabulary with observed dominant (domain, range).

Two tables of overlap with what we already have:
  - `lineage_edge` is ABox (concrete asset → asset edges). Predicate-class
    schema lives nowhere today. This migration adds it.
  - `card` already stores concept definitions (object_type, causal_node, …).
    `relation_type` references those by `card_id` for `domain` / `range_type`
    when the dominant types resolve to authored cards. When they don't
    (e.g. corpus produced an unmodelled subject type), the column simply
    holds the surface string.

Schema:
  - Natural key: `(org_id, predicate, domain, range_type)`. Re-running
    induction overwrites in place.
  - `provenance` records who proposed the row (`induce_schema` for foundry,
    `manual` for hand-authored).
  - `evidence_count` carries `InducedPredicate.support` so retrieval can
    filter by minimum support.

`lineage_edge.predicate_id` is nullable — existing rows stay null. A
post-induction pass updates the column for edges whose `evidence_ref` /
`edge_kind` matches a canonicalized predicate.

Revision ID: 0008_add_relation_type
Revises: 0007_add_column_stats
Create Date: 2026-05-18
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008_add_relation_type"
down_revision: Union[str, None] = "0007_add_column_stats"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "relation_type",
        sa.Column("relation_type_pk", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("predicate", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("range_type", sa.Text(), nullable=False),
        sa.Column("inverse", sa.Text(), nullable=True),
        sa.Column("functional", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("surfaces", sa.Text(), nullable=True,
                  comment="Comma-separated original surface predicates that canonicalized to this row."),
        sa.Column(
            "provenance", sa.Text(), nullable=False,
            server_default="induce_schema",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organization.org_id"], ondelete="CASCADE",
            name="fk_relation_type_org",
        ),
        sa.UniqueConstraint(
            "org_id", "predicate", "domain", "range_type",
            name="uq_relation_type_natural_key",
        ),
    )
    op.create_index(
        "idx_relation_type_predicate", "relation_type", ["predicate"],
    )
    op.create_index(
        "idx_relation_type_org_pred", "relation_type", ["org_id", "predicate"],
    )

    # Link existing ABox rows to the new TBox.
    op.add_column(
        "lineage_edge",
        sa.Column("predicate_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_lineage_edge_predicate",
        source_table="lineage_edge",
        referent_table="relation_type",
        local_cols=["predicate_id"],
        remote_cols=["relation_type_pk"],
        ondelete="SET NULL",
    )
    op.create_index(
        "idx_lineage_edge_predicate", "lineage_edge", ["predicate_id"],
        postgresql_where=sa.text("predicate_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_lineage_edge_predicate", table_name="lineage_edge")
    op.drop_constraint("fk_lineage_edge_predicate", "lineage_edge", type_="foreignkey")
    op.drop_column("lineage_edge", "predicate_id")
    op.drop_index("idx_relation_type_org_pred", table_name="relation_type")
    op.drop_index("idx_relation_type_predicate", table_name="relation_type")
    op.drop_table("relation_type")
