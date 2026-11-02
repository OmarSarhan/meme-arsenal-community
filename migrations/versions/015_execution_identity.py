"""Nullable execution identity and derivative hashes; no historical inference."""
from alembic import op

from meme_arsenal.schema_015 import additive_columns_015

revision = "015_execution_identity"
down_revision = "014_source_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table_name, column in additive_columns_015():
        op.add_column(table_name, column)


def downgrade() -> None:
    for table_name, column in reversed(additive_columns_015()):
        op.drop_column(table_name, column.name)
