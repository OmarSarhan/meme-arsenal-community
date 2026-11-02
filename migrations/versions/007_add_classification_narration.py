"""Add classification narration column.

Revision ID: 007_add_classification_narration
Revises: 006_add_remix_rejection_reasons
Create Date: 2026-07-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "007_add_classification_narration"
down_revision = "006_add_remix_rejection_reasons"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    return column in {row["name"] for row in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _column_exists("classifications", "narration"):
        op.add_column(
            "classifications",
            sa.Column("narration", sa.Text(), nullable=False, server_default=""),
        )


def downgrade() -> None:
    if _column_exists("classifications", "narration"):
        op.drop_column("classifications", "narration")
