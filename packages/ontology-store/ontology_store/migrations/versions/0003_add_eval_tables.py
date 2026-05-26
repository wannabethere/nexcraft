"""Add eval tables — eval_case, eval_run, eval_result, eval_metric.

Revision ID: 0003_add_eval_tables
Revises: 0002_add_reindex_queue
Create Date: 2026-05-17
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


revision: str = "0003_add_eval_tables"
down_revision: Union[str, None] = "0002_add_reindex_queue"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "eval_case",
        sa.Column("case_id", sa.Text(), primary_key=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("intent", sa.Text()),
        sa.Column("expected_anchors", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("expected_asset_rks", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("forbidden_asset_rks", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("scope_payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column("retrieval_kind_default", sa.Text()),
        sa.Column("hardness", sa.Text(), nullable=False, server_default="medium"),
        sa.Column("domain_tags", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("authored_by", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_eval_case_enabled", "eval_case", ["enabled"])
    op.create_index("idx_eval_case_hardness", "eval_case", ["hardness"])

    op.create_table(
        "eval_run",
        sa.Column("run_id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("retrieval_kind", sa.Text(), nullable=False),
        sa.Column("scorer_names", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("case_filter", JSONB(), nullable=False, server_default="{}"),
        sa.Column("case_count", sa.Integer()),
        sa.Column("passed_count", sa.Integer()),
        sa.Column("trigger", sa.Text(), nullable=False, server_default="manual"),
        sa.Column("metadata", JSONB()),
        sa.Column("last_error", sa.Text()),
    )
    op.create_index("idx_eval_run_status", "eval_run", ["status"])
    op.create_index("idx_eval_run_started", "eval_run", ["started_at"])

    op.create_table(
        "eval_result",
        sa.Column("result_id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("eval_run.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("case_id", sa.Text(), sa.ForeignKey("eval_case.case_id", ondelete="CASCADE"), nullable=False),
        sa.Column("scorer_name", sa.Text(), nullable=False),
        sa.Column("retrieved_rks", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("metrics", JSONB(), nullable=False, server_default="{}"),
        sa.Column("llm_judgment", JSONB()),
        sa.Column("pass_gate", sa.Boolean()),
        sa.Column("notes", sa.Text()),
        sa.Column("wall_time_ms", sa.Integer()),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_eval_result_run", "eval_result", ["run_id"])
    op.create_index("idx_eval_result_case", "eval_result", ["case_id"])
    op.create_index("idx_eval_result_scorer", "eval_result", ["scorer_name"])

    op.create_table(
        "eval_metric",
        sa.Column("metric_id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("eval_run.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("scorer_name", sa.Text()),
        sa.Column("metric_name", sa.Text(), nullable=False),
        sa.Column("metric_value", sa.Float()),
        sa.Column("cardinality", sa.Integer()),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_eval_metric_run", "eval_metric", ["run_id"])
    op.create_index("idx_eval_metric_name", "eval_metric", ["metric_name"])


def downgrade() -> None:
    op.drop_index("idx_eval_metric_name", table_name="eval_metric")
    op.drop_index("idx_eval_metric_run", table_name="eval_metric")
    op.drop_table("eval_metric")
    op.drop_index("idx_eval_result_scorer", table_name="eval_result")
    op.drop_index("idx_eval_result_case", table_name="eval_result")
    op.drop_index("idx_eval_result_run", table_name="eval_result")
    op.drop_table("eval_result")
    op.drop_index("idx_eval_run_started", table_name="eval_run")
    op.drop_index("idx_eval_run_status", table_name="eval_run")
    op.drop_table("eval_run")
    op.drop_index("idx_eval_case_hardness", table_name="eval_case")
    op.drop_index("idx_eval_case_enabled", table_name="eval_case")
    op.drop_table("eval_case")
