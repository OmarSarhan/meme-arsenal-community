"""Initial Meme Arsenal schema.

Revision ID: 001_initial_schema
Revises:
Create Date: 2026-06-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = sa.JSON()
    op.create_table(
        "telegram_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_message_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("grouped_id", sa.String(length=128), nullable=True),
        sa.Column("forwarded_from", sa.Text(), nullable=True),
        sa.Column("telegram_post_url", sa.Text(), nullable=True),
        sa.Column("raw_metadata_json", json_type, nullable=True),
        sa.Column("import_status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("telegram_message_id", "chat_id", name="uq_telegram_message"),
    )
    op.create_index("ix_telegram_messages_telegram_message_id", "telegram_messages", ["telegram_message_id"])
    op.create_index("ix_telegram_messages_chat_id", "telegram_messages", ["chat_id"])
    op.create_index("ix_telegram_messages_grouped_id", "telegram_messages", ["grouped_id"])
    op.create_index("ix_telegram_messages_import_status", "telegram_messages", ["import_status"])

    op.create_table(
        "media_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_message_id", sa.Integer(), sa.ForeignKey("telegram_messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=True),
        sa.Column("original_path", sa.Text(), nullable=True),
        sa.Column("thumbnail_path", sa.Text(), nullable=True),
        sa.Column("preview_path", sa.Text(), nullable=True),
        sa.Column("hover_preview_path", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("perceptual_hash", sa.String(length=64), nullable=True),
        sa.Column("duplicate_group_id", sa.String(length=96), nullable=True),
        sa.Column("processing_status", sa.String(length=32), nullable=False),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_media_items_telegram_message_id", "media_items", ["telegram_message_id"])
    op.create_index("ix_media_items_media_type", "media_items", ["media_type"])
    op.create_index("ix_media_items_sha256", "media_items", ["sha256"])
    op.create_index("ix_media_items_perceptual_hash", "media_items", ["perceptual_hash"])
    op.create_index("ix_media_items_duplicate_group_id", "media_items", ["duplicate_group_id"])
    op.create_index("ix_media_items_processing_status", "media_items", ["processing_status"])

    op.create_table(
        "media_keyframes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("media_item_id", sa.Integer(), sa.ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("frame_index", sa.Integer(), nullable=False),
        sa.Column("timestamp_seconds", sa.Float(), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.UniqueConstraint("media_item_id", "frame_index", name="uq_media_keyframe_index"),
    )
    op.create_index("ix_media_keyframes_media_item_id", "media_keyframes", ["media_item_id"])

    op.create_table(
        "classifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("media_item_id", sa.Integer(), sa.ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("visible_text", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("meme_format", sa.String(length=128), nullable=True),
        sa.Column("tone", json_type, nullable=True),
        sa.Column("reaction_type", json_type, nullable=True),
        sa.Column("topics_json", json_type, nullable=True),
        sa.Column("keywords_json", json_type, nullable=True),
        sa.Column("people_or_characters_json", json_type, nullable=True),
        sa.Column("safety_flags_json", json_type, nullable=True),
        sa.Column("search_phrases_json", json_type, nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("raw_response_json", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("media_item_id", "model", "prompt_version", name="uq_classification_version"),
    )
    op.create_index("ix_classifications_media_item_id", "classifications", ["media_item_id"])
    op.create_index("ix_classifications_meme_format", "classifications", ["meme_format"])

    op.create_table(
        "embeddings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("media_item_id", sa.Integer(), sa.ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("embedding_source", sa.String(length=64), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding_json", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("media_item_id", "embedding_model", "embedding_source", name="uq_embedding"),
    )
    op.create_index("ix_embeddings_media_item_id", "embeddings", ["media_item_id"])
    op.create_index("ix_embeddings_embedding_model", "embeddings", ["embedding_model"])

    op.create_table(
        "curation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("media_item_id", sa.Integer(), sa.ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("favorite", sa.Boolean(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("manual_tags_json", json_type, nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("media_item_id", name="uq_curation_media_item"),
    )
    op.create_index("ix_curation_media_item_id", "curation", ["media_item_id"])
    op.create_index("ix_curation_favorite", "curation", ["favorite"])
    op.create_index("ix_curation_archived", "curation", ["archived"])

    op.create_table(
        "import_checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("last_message_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "chat_id", name="uq_import_checkpoint"),
    )

    op.create_table(
        "import_failures",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_message_id", sa.Integer(), nullable=True),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_import_failures_telegram_message_id", "import_failures", ["telegram_message_id"])
    op.create_index("ix_import_failures_stage", "import_failures", ["stage"])
    op.create_index("ix_import_failures_status", "import_failures", ["status"])


def downgrade() -> None:
    for table in [
        "import_failures",
        "import_checkpoints",
        "curation",
        "embeddings",
        "classifications",
        "media_keyframes",
        "media_items",
        "telegram_messages",
    ]:
        op.drop_table(table)
