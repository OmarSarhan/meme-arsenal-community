"""Add permanent five-character aliases for public shares.

Revision ID: 012_public_share_codes
Revises: 011_public_media_shares
"""

from alembic import op
import sqlalchemy as sa


revision = "012_public_share_codes"
down_revision = "011_public_media_shares"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "public_share_codes",
        sa.Column("code", sa.String(5), primary_key=True),
        sa.Column("token_id", sa.String(64), nullable=False),
        sa.Column("card", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("token_id", "card", name="uq_public_share_code_variant"),
    )


def downgrade() -> None:
    op.drop_table("public_share_codes")
