"""Add video hover preview path.

Revision ID: 002_add_hover_preview_path
Revises: 001_initial_schema
Create Date: 2026-06-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "002_add_hover_preview_path"
down_revision = "001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("media_items")}
    if "hover_preview_path" not in columns:
        op.add_column("media_items", sa.Column("hover_preview_path", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("media_items")}
    if "hover_preview_path" in columns:
        op.drop_column("media_items", "hover_preview_path")
