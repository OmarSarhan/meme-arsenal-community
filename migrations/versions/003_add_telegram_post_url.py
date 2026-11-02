"""Add Telegram post URL to messages.

Revision ID: 003_add_telegram_post_url
Revises: 002_add_hover_preview_path
Create Date: 2026-06-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "003_add_telegram_post_url"
down_revision = "002_add_hover_preview_path"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("telegram_messages")}
    if "telegram_post_url" not in columns:
        op.add_column("telegram_messages", sa.Column("telegram_post_url", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("telegram_messages")}
    if "telegram_post_url" in columns:
        op.drop_column("telegram_messages", "telegram_post_url")
