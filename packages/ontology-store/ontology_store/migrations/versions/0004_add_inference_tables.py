"""Add inference tables — causal_candidate, data_protection_hint.

Revision ID: 0004_add_inference_tables
Revises: 0003_add_eval_tables
Create Date: 2026-05-17
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


revision: str = "0004_add_inference_tables"
down_revision: Union[str, None] = "0003_add_eval_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "causal_candidate",
        sa.Column("candidate_id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("asset_rk", sa.Text(), nullable=False),
        sa.Column("subject_ref", sa.Text(), nullable=False),
        sa.Column("predicate", sa.Text(), nullable=False),
        sa.Column("object_ref", sa.Text(), nullable=False),
        sa.Column("evidence_columns", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("mechanism_hint", sa.Text()),
        sa.Column("confidence", sa.Float()),
        sa.Column("status", sa.Text(), nullable=False, server_default="proposed"),
        sa.Column("provenance", sa.Text(), nullable=False, server_default="llm_causal_dependency"),
        sa.Column("promoted_to_claim_id", sa.Text()),
        sa.Column("rationale", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "asset_rk", "subject_ref", "predicate", "object_ref",
            name="uq_causal_candidate_natural_key",
        ),
    )
    op.create_index("idx_causal_candidate_asset", "causal_candidate", ["asset_rk"])
    op.create_index("idx_causal_candidate_status", "causal_candidate", ["status"])
    op.create_index("idx_causal_candidate_object", "causal_candidate", ["object_ref"])

    op.create_table(
        "data_protection_hint",
        sa.Column("hint_id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("asset_rk", sa.Text(), nullable=False),
        sa.Column("rls_predicates", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("cls_columns", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("rationale", sa.Text()),
        sa.Column("provenance", sa.Text(), nullable=False, server_default="llm_data_protection"),
        sa.Column("status", sa.Text(), nullable=False, server_default="proposed"),
        sa.Column("extra", JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "asset_rk", "provenance",
            name="uq_data_protection_hint_asset_provenance",
        ),
    )
    op.create_index("idx_data_protection_hint_asset", "data_protection_hint", ["asset_rk"])
    op.create_index("idx_data_protection_hint_status", "data_protection_hint", ["status"])


def downgrade() -> None:
    op.drop_index("idx_data_protection_hint_status", table_name="data_protection_hint")
    op.drop_index("idx_data_protection_hint_asset", table_name="data_protection_hint")
    op.drop_table("data_protection_hint")
    op.drop_index("idx_causal_candidate_object", table_name="causal_candidate")
    op.drop_index("idx_causal_candidate_status", table_name="causal_candidate")
    op.drop_index("idx_causal_candidate_asset", table_name="causal_candidate")
    op.drop_table("causal_candidate")
