"""Nullable remix generation capture; historical jobs remain unknown."""

from alembic import op

from meme_arsenal.schema_016 import additive_columns_016

revision = "016_remix_execution"
down_revision = "015_execution_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table_name, column in additive_columns_016():
        op.add_column(table_name, column)


def downgrade() -> None:
    for table_name, column in reversed(additive_columns_016()):
        op.drop_column(table_name, column.name)
