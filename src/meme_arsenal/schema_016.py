"""Frozen nullable generation capture definitions for revision 016."""

import sqlalchemy as sa


def additive_columns_016():
    return (
        ("remix_jobs", sa.Column("execution_capture_json", sa.JSON(), nullable=True)),
        ("remix_jobs", sa.Column("execution_first_captured_attempt", sa.Integer(), nullable=True)),
    )
