"""Persistent identity for immutable folder snapshots; no legacy backfill."""
from alembic import op
import sqlalchemy as sa

revision = "014_source_identity"
down_revision = "013_explicit_recipe_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("namespace", sa.String(128), nullable=False),
        sa.Column("external_key", sa.String(1024), nullable=False),
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("anonymous_sessions.id", ondelete="RESTRICT")),
        sa.Column("media_item_id", sa.Integer(), sa.ForeignKey("media_items.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.UniqueConstraint("kind", "namespace", "external_key", name="uq_source_identity"),
        sa.CheckConstraint("kind = 'folder'", name="ck_source_kind"),
        sa.CheckConstraint("length(namespace) > 0 AND length(namespace) <= 128", name="ck_source_namespace"),
        sa.CheckConstraint("length(external_key) > 0 AND length(external_key) <= 1024", name="ck_source_key"),
    )


def downgrade() -> None:
    op.drop_table("source_identities")
