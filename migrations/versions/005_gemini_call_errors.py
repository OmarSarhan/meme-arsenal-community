"""Add Gemini call error audit log.

Revision ID: 005_gemini_call_errors
Revises: 004_online_public_schema
Create Date: 2026-06-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "005_gemini_call_errors"
down_revision = "004_online_public_schema"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _index_exists(table: str, name: str) -> bool:
    return name in {index["name"] for index in inspect(op.get_bind()).get_indexes(table)}


def _create_index_if_missing(name: str, table: str, columns: list[str]) -> None:
    if not _index_exists(table, name):
        op.create_index(name, table, columns)


def upgrade() -> None:
    json_type = sa.JSON()

    if not _table_exists("gemini_call_errors"):
        op.create_table(
            "gemini_call_errors",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("model", sa.String(length=128), nullable=False),
            sa.Column("method", sa.String(length=64), nullable=False),
            sa.Column("url", sa.Text(), nullable=False, server_default=""),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("will_retry", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("status_code", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(length=64), nullable=True),
            sa.Column("error_type", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
            sa.Column("raw_body", sa.Text(), nullable=False, server_default=""),
            sa.Column("retry_after_seconds", sa.Float(), nullable=True),
            sa.Column("quota_metric", sa.Text(), nullable=True),
            sa.Column("request_json", json_type, nullable=True),
            sa.Column("response_json", json_type, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

    _create_index_if_missing("ix_gemini_call_errors_model", "gemini_call_errors", ["model"])
    _create_index_if_missing("ix_gemini_call_errors_method", "gemini_call_errors", ["method"])
    _create_index_if_missing("ix_gemini_call_errors_will_retry", "gemini_call_errors", ["will_retry"])
    _create_index_if_missing("ix_gemini_call_errors_status_code", "gemini_call_errors", ["status_code"])
    _create_index_if_missing("ix_gemini_call_errors_status", "gemini_call_errors", ["status"])
    _create_index_if_missing("ix_gemini_call_errors_created", "gemini_call_errors", ["created_at"])
    _create_index_if_missing(
        "ix_gemini_call_errors_model_method_created",
        "gemini_call_errors",
        ["model", "method", "created_at"],
    )
    _create_index_if_missing(
        "ix_gemini_call_errors_status_created",
        "gemini_call_errors",
        ["status_code", "created_at"],
    )


def downgrade() -> None:
    if _table_exists("gemini_call_errors"):
        op.drop_table("gemini_call_errors")
