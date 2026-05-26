"""Initial schema — create all tables defined in ontology_store.db.models.

This is the only migration in v1; subsequent schema changes get incremental
Alembic revisions. The migration body uses `op.create_all` via the model
metadata so the migration code stays in sync with the ORM models automatically.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-17
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from ontology_store.db.engine import Base
from ontology_store.db import models  # noqa: F401 — registers tables in metadata


revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
