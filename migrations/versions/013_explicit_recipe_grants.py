"""Recall legacy capabilities and require explicit recipe consent.

All legacy output links also stop. Aliases remain reserved permanently.
"""
from alembic import op
import sqlalchemy as sa

revision = "013_explicit_recipe_grants"
down_revision = "012_public_share_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("public_media_shares", sa.Column("recipe_grant_json", sa.JSON(none_as_null=True), nullable=True))
    op.execute("UPDATE public_media_shares SET active = false, revoked_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP")


def downgrade() -> None:
    op.drop_column("public_media_shares", "recipe_grant_json")
