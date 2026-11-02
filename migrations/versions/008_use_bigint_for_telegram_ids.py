"""Use bigint for external Telegram message identifiers.

Revision ID: 008_use_bigint_for_telegram_ids
Revises: 007_add_classification_narration
Create Date: 2026-07-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "008_use_bigint_for_telegram_ids"
down_revision = "007_add_classification_narration"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    return column in {row["name"] for row in inspect(op.get_bind()).get_columns(table)}


def _alter_column(
    table: str,
    column: str,
    type_: sa.TypeEngine,
    existing_type: sa.TypeEngine,
    *,
    nullable: bool,
) -> None:
    if not _column_exists(table, column):
        return
    with op.batch_alter_table(table) as batch:
        batch.alter_column(column, type_=type_, existing_type=existing_type, existing_nullable=nullable)


def upgrade() -> None:
    _alter_column("telegram_messages", "telegram_message_id", sa.BigInteger(), sa.Integer(), nullable=False)
    _alter_column("import_checkpoints", "last_message_id", sa.BigInteger(), sa.Integer(), nullable=False)
    _alter_column("import_failures", "telegram_message_id", sa.BigInteger(), sa.Integer(), nullable=True)


def downgrade() -> None:
    _alter_column("import_failures", "telegram_message_id", sa.Integer(), sa.BigInteger(), nullable=True)
    _alter_column("import_checkpoints", "last_message_id", sa.Integer(), sa.BigInteger(), nullable=False)
    _alter_column("telegram_messages", "telegram_message_id", sa.Integer(), sa.BigInteger(), nullable=False)
