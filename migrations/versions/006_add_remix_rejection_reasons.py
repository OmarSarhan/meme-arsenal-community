"""Add concise remix rejection reasons.

Revision ID: 006_add_remix_rejection_reasons
Revises: 005_gemini_call_errors
Create Date: 2026-06-22
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "006_add_remix_rejection_reasons"
down_revision = "005_gemini_call_errors"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _column_exists(table: str, name: str) -> bool:
    return name in {column["name"] for column in inspect(op.get_bind()).get_columns(table)}


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if _table_exists(table) and not _column_exists(table, column.name):
        op.add_column(table, column)


def upgrade() -> None:
    _add_column_if_missing("remix_jobs", sa.Column("rejection_reason", sa.Text(), nullable=True))
    _add_column_if_missing("remix_attempts", sa.Column("rejection_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    if _table_exists("remix_attempts") and _column_exists("remix_attempts", "rejection_reason"):
        op.drop_column("remix_attempts", "rejection_reason")
    if _table_exists("remix_jobs") and _column_exists("remix_jobs", "rejection_reason"):
        op.drop_column("remix_jobs", "rejection_reason")
