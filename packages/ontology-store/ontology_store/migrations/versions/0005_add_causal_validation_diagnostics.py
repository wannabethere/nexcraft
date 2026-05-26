"""Add validation_diagnostics JSONB column to causal_candidate.

The statistical-validation worker (ontology-pipeline.validate.causal_validation)
runs ontology_foundry.causal methods against sample data from the source for
each proposed causal_candidate row and writes the resulting p-values,
algorithms-agreed list, sample size, and decision rationale into this column.
Operators read it to understand WHY a candidate was validated/rejected.

Revision ID: 0005_add_causal_validation_diagnostics
Revises: 0004_add_inference_tables
Create Date: 2026-05-17
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0005_add_causal_validation_diagnostics"
down_revision: Union[str, None] = "0004_add_inference_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "causal_candidate",
        sa.Column("validation_diagnostics", JSONB(), nullable=True),
    )
    op.add_column(
        "causal_candidate",
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial index — quickly find rows that still need a validation pass.
    op.create_index(
        "idx_causal_candidate_pending_validation",
        "causal_candidate",
        ["asset_rk"],
        postgresql_where=sa.text("status = 'proposed'"),
    )


def downgrade() -> None:
    op.drop_index("idx_causal_candidate_pending_validation", table_name="causal_candidate")
    op.drop_column("causal_candidate", "validated_at")
    op.drop_column("causal_candidate", "validation_diagnostics")
