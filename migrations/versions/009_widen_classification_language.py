"""Widen classification language labels.

Revision ID: 009_widen_language
Revises: 008_use_bigint_for_telegram_ids
Create Date: 2026-07-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "009_widen_language"
down_revision = "008_use_bigint_for_telegram_ids"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    return column in {row["name"] for row in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _column_exists("classifications", "language"):
        return
    with op.batch_alter_table("classifications") as batch:
        batch.alter_column(
            "language",
            type_=sa.String(length=64),
            existing_type=sa.String(length=16),
            existing_nullable=True,
        )


def downgrade() -> None:
    if not _column_exists("classifications", "language"):
        return
    with op.batch_alter_table("classifications") as batch:
        batch.alter_column(
            "language",
            type_=sa.String(length=16),
            existing_type=sa.String(length=64),
            existing_nullable=True,
        )
