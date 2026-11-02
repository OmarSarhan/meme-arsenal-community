"""Add online public site schema.

Revision ID: 004_online_public_schema
Revises: 003_add_telegram_post_url
Create Date: 2026-06-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "004_online_public_schema"
down_revision = "003_add_telegram_post_url"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _column_exists(table: str, column: str) -> bool:
    return column in {row["name"] for row in inspect(op.get_bind()).get_columns(table)}


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if not _column_exists(table, column.name):
        op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    json_type = sa.JSON()

    if dialect == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    _add_column_if_missing(
        "media_items",
        sa.Column("public_status", sa.String(length=32), nullable=False, server_default="published"),
    )
    _add_column_if_missing("media_items", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
    _add_column_if_missing(
        "media_items", sa.Column("origin", sa.String(length=32), nullable=False, server_default="telegram")
    )
    _add_column_if_missing(
        "media_items", sa.Column("visibility", sa.String(length=32), nullable=False, server_default="public")
    )
    _add_column_if_missing(
        "media_items",
        sa.Column("public_original_allowed", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    if not _column_exists("embeddings", "embedding_vector"):
        if dialect == "postgresql":
            op.execute("ALTER TABLE embeddings ADD COLUMN embedding_vector vector")
        else:
            op.add_column("embeddings", sa.Column("embedding_vector", sa.Text(), nullable=True))

    if dialect == "postgresql":
        op.execute(
            "UPDATE media_items SET published_at = COALESCE(published_at, created_at, now()) "
            "WHERE public_status = 'published'"
        )
    else:
        op.execute(
            "UPDATE media_items SET published_at = COALESCE(published_at, created_at) "
            "WHERE public_status = 'published'"
        )

    for table, column in [
        ("media_items", "public_status"),
        ("media_items", "published_at"),
        ("media_items", "origin"),
        ("media_items", "visibility"),
        ("media_items", "public_original_allowed"),
    ]:
        index_name = f"ix_{table}_{column}"
        if index_name not in {index["name"] for index in inspect(bind).get_indexes(table)}:
            op.create_index(index_name, table, [column])

    if not _table_exists("anonymous_sessions"):
        op.create_table(
            "anonymous_sessions",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("ip_hash", sa.String(length=128), nullable=True),
            sa.Column("user_agent_hash", sa.String(length=128), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_anonymous_sessions_ip_hash", "anonymous_sessions", ["ip_hash"])
        op.create_index("ix_anonymous_sessions_last_seen_at", "anonymous_sessions", ["last_seen_at"])

    if not _table_exists("anonymous_session_curation"):
        op.create_table(
            "anonymous_session_curation",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "session_id",
                sa.String(length=64),
                sa.ForeignKey("anonymous_sessions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "media_item_id",
                sa.Integer(),
                sa.ForeignKey("media_items.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("favorite", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("manual_tags_json", json_type, nullable=True),
            sa.Column("notes", sa.Text(), nullable=False, server_default=""),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("session_id", "media_item_id", name="uq_session_media_curation"),
        )
        op.create_index("ix_anonymous_session_curation_session_id", "anonymous_session_curation", ["session_id"])
        op.create_index("ix_anonymous_session_curation_media_item_id", "anonymous_session_curation", ["media_item_id"])
        op.create_index("ix_anonymous_session_curation_favorite", "anonymous_session_curation", ["favorite"])
        op.create_index("ix_anonymous_session_curation_archived", "anonymous_session_curation", ["archived"])
        op.create_index(
            "ix_session_curation_session_updated",
            "anonymous_session_curation",
            ["session_id", "updated_at"],
        )

    if not _table_exists("media_tags"):
        op.create_table(
            "media_tags",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "media_item_id",
                sa.Integer(),
                sa.ForeignKey("media_items.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("tag_type", sa.String(length=64), nullable=False),
            sa.Column("value", sa.String(length=255), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False, server_default="classification"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("media_item_id", "tag_type", "value", "source", name="uq_media_tag"),
        )
        op.create_index("ix_media_tags_media_item_id", "media_tags", ["media_item_id"])
        op.create_index("ix_media_tags_lookup", "media_tags", ["tag_type", "value", "media_item_id"])

    if not _table_exists("public_search_documents"):
        op.create_table(
            "public_search_documents",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "media_item_id",
                sa.Integer(),
                sa.ForeignKey("media_items.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("document_text", sa.Text(), nullable=False, server_default=""),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("media_item_id", name="uq_public_search_document_media"),
        )
        op.create_index("ix_public_search_documents_media_item_id", "public_search_documents", ["media_item_id"])
        op.create_index("ix_public_search_documents_updated_at", "public_search_documents", ["updated_at"])

    if dialect == "postgresql":
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_public_search_documents_fts "
            "ON public_search_documents USING gin (to_tsvector('simple', document_text))"
        )

    if not _table_exists("remix_jobs"):
        op.create_table(
            "remix_jobs",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column(
                "session_id",
                sa.String(length=64),
                sa.ForeignKey("anonymous_sessions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("left_id", sa.Integer(), sa.ForeignKey("media_items.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("right_id", sa.Integer(), sa.ForeignKey("media_items.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("safety_mode", sa.String(length=32), nullable=False, server_default="standard"),
            sa.Column("tweaks_json", json_type, nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
            sa.Column("message", sa.Text(), nullable=False, server_default="Queued"),
            sa.Column("retry_after_seconds", sa.Float(), nullable=True),
            sa.Column("block_reason", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("result_json", json_type, nullable=True),
            sa.Column(
                "saved_media_item_id",
                sa.Integer(),
                sa.ForeignKey("media_items.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("embedding_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_remix_jobs_session_id", "remix_jobs", ["session_id"])
        op.create_index("ix_remix_jobs_status", "remix_jobs", ["status"])
        op.create_index("ix_remix_jobs_saved_media_item_id", "remix_jobs", ["saved_media_item_id"])
        op.create_index("ix_remix_jobs_created_at", "remix_jobs", ["created_at"])
        op.create_index("ix_remix_jobs_session_created", "remix_jobs", ["session_id", "created_at"])
        op.create_index("ix_remix_jobs_status_updated", "remix_jobs", ["status", "updated_at"])

    if not _table_exists("remix_attempts"):
        op.create_table(
            "remix_attempts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("job_id", sa.String(length=64), sa.ForeignKey("remix_jobs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("mitigation_id", sa.String(length=128), nullable=False, server_default="original"),
            sa.Column("label", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
            sa.Column("message", sa.Text(), nullable=False, server_default=""),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("block_reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("job_id", "attempt", name="uq_remix_attempt_number"),
        )
        op.create_index("ix_remix_attempts_job_id", "remix_attempts", ["job_id"])
        op.create_index("ix_remix_attempts_status", "remix_attempts", ["status"])
        op.create_index("ix_remix_attempts_job_created", "remix_attempts", ["job_id", "created_at"])

    if not _table_exists("remix_sources"):
        op.create_table(
            "remix_sources",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("job_id", sa.String(length=64), sa.ForeignKey("remix_jobs.id", ondelete="SET NULL"), nullable=True),
            sa.Column(
                "remix_media_item_id",
                sa.Integer(),
                sa.ForeignKey("media_items.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "source_media_item_id",
                sa.Integer(),
                sa.ForeignKey("media_items.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("remix_media_item_id", "source_media_item_id", name="uq_remix_source_link"),
        )
        op.create_index("ix_remix_sources_job_id", "remix_sources", ["job_id"])
        op.create_index("ix_remix_sources_remix_media_item_id", "remix_sources", ["remix_media_item_id"])
        op.create_index("ix_remix_sources_source_media_item_id", "remix_sources", ["source_media_item_id"])
        op.create_index("ix_remix_sources_source", "remix_sources", ["source_media_item_id"])


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_public_search_documents_fts")
    for table in [
        "remix_sources",
        "remix_attempts",
        "remix_jobs",
        "public_search_documents",
        "media_tags",
        "anonymous_session_curation",
        "anonymous_sessions",
    ]:
        if _table_exists(table):
            op.drop_table(table)
    for column in [
        "embedding_vector",
    ]:
        if _column_exists("embeddings", column):
            op.drop_column("embeddings", column)
    for column in [
        "public_original_allowed",
        "visibility",
        "origin",
        "published_at",
        "public_status",
    ]:
        if _column_exists("media_items", column):
            op.drop_column("media_items", column)
