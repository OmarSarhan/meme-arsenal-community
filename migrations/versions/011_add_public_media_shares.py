"""Add revocable public media share links.

Revision ID: 011_public_media_shares
Revises: 010_ai_provider_circuit
Create Date: 2026-08-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "011_public_media_shares"
down_revision = "010_ai_provider_circuit"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _table_exists("public_media_shares"):
        return

    op.create_table(
        "public_media_shares",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "media_item_id",
            sa.Integer(),
            sa.ForeignKey("media_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_id", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("media_item_id", name="uq_public_media_share_media"),
        sa.UniqueConstraint("token_id", name="uq_public_media_share_token"),
    )
    op.create_index(
        "ix_public_media_shares_media_item_id",
        "public_media_shares",
        ["media_item_id"],
    )
    op.create_index(
        "ix_public_media_shares_active_updated",
        "public_media_shares",
        ["active", "updated_at"],
    )


def downgrade() -> None:
    if _table_exists("public_media_shares"):
        op.drop_table("public_media_shares")
