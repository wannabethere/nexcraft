"""Add card + card_ref tables — Postgres-backed semantic-layer cards.

Cards (object_type, causal_node, key_area, …) move from filesystem-only
storage into Postgres so that:

  - Enrichment stages (CausalDependencyEnricher, AnnotationEnricher) can load
    tenant vocabulary by SQL instead of scanning a directory tree per run.
  - Card updates have an audit + content_hash trail.
  - The vector indexer reads from a single source of truth.

The filesystem-based loader still works — it now syncs INTO this table rather
than feeding the pipeline directly.

Revision ID: 0006_add_card_storage
Revises: 0005_add_causal_validation_diagnostics
Create Date: 2026-05-17
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


revision: str = "0006_add_card_storage"
down_revision: Union[str, None] = "0005_add_causal_validation_diagnostics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "card",
        sa.Column("card_pk", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column(
            "org_id", sa.Text(), nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("card_id", sa.Text(), nullable=False),
        sa.Column("layer", sa.Text(), nullable=False, server_default="semantic"),
        sa.Column("title", sa.Text()),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("frontmatter", JSONB()),
        sa.Column("aliases", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("markings", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("origin", sa.Text(), nullable=False, server_default="tenant"),
        sa.Column("deprecated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deprecated_at", sa.DateTime(timezone=True)),
        sa.Column("source_path", sa.Text()),
        sa.Column("content_hash", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organization.org_id"], ondelete="CASCADE",
            name="fk_card_org",
        ),
        sa.UniqueConstraint("org_id", "kind", "card_id", name="uq_card_natural_key"),
        # Recognised kinds — keep in sync with KNOWN_CARD_KINDS in card_models.py.
        # We use a CHECK rather than ENUM so adding a kind is just a code change.
        sa.CheckConstraint(
            "kind IN ('object_type','interface','causal_node','derived_state',"
            "'action','metric','event','instruction','key_area')",
            name="ck_card_kind_known",
        ),
    )
    op.create_index("idx_card_org_kind", "card", ["org_id", "kind"])
    op.create_index("idx_card_card_id", "card", ["card_id"])
    op.create_index(
        "idx_card_active",
        "card",
        ["org_id", "kind"],
        postgresql_where=sa.text("deprecated = false"),
    )

    op.create_table(
        "card_ref",
        sa.Column("ref_id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("from_card_pk", sa.Integer(), nullable=False),
        sa.Column("to_kind", sa.Text(), nullable=False),
        sa.Column("to_card_id", sa.Text(), nullable=False),
        sa.Column("relation", sa.Text(), nullable=False, server_default="mentions"),
        sa.Column("extra", JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["from_card_pk"], ["card.card_pk"], ondelete="CASCADE",
            name="fk_card_ref_from_card",
        ),
        sa.UniqueConstraint(
            "from_card_pk", "to_kind", "to_card_id", "relation",
            name="uq_card_ref_natural_key",
        ),
    )
    op.create_index("idx_card_ref_to", "card_ref", ["to_kind", "to_card_id"])


def downgrade() -> None:
    op.drop_index("idx_card_ref_to", table_name="card_ref")
    op.drop_table("card_ref")
    op.drop_index("idx_card_active", table_name="card")
    op.drop_index("idx_card_card_id", table_name="card")
    op.drop_index("idx_card_org_kind", table_name="card")
    op.drop_table("card")
