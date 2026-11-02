"""Frozen additive definitions for revision 015; never derive these from models."""

import sqlalchemy as sa


def additive_columns_015():
    """Return fresh columns; retain these definitions for historical upgrades."""
    return (
        ("classifications", sa.Column("execution_provider", sa.String(32), nullable=True)),
        ("classifications", sa.Column("compatibility_key", sa.String(64), nullable=True)),
        ("embeddings", sa.Column("execution_provider", sa.String(32), nullable=True)),
        ("embeddings", sa.Column("source_text_hash", sa.String(64), nullable=True)),
        ("media_items", sa.Column("derivative_hashes_json", sa.JSON(), nullable=True)),
    )
