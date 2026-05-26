"""Add the reindex_queue table.

The workers/queue.py model uses `Base` via `ontology_store.workers.queue` —
when Alembic autogenerate runs against the metadata, the table appears. This
explicit revision lets us deploy without forcing a full create_all sweep.

Revision ID: 0002_add_reindex_queue
Revises: 0001_initial_schema
Create Date: 2026-05-17
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0002_add_reindex_queue"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reindex_queue",
        sa.Column("queue_id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("task_kind", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
    )
    # Partial index supporting cheap `SELECT ... WHERE status = 'pending' ORDER BY enqueued_at`
    op.create_index(
        "idx_reindex_queue_pending",
        "reindex_queue",
        ["enqueued_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "idx_reindex_queue_task_kind",
        "reindex_queue",
        ["task_kind", "status"],
    )


def downgrade() -> None:
    op.drop_index("idx_reindex_queue_task_kind", table_name="reindex_queue")
    op.drop_index("idx_reindex_queue_pending", table_name="reindex_queue")
    op.drop_table("reindex_queue")
