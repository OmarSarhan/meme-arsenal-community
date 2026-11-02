from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON
from sqlalchemy.types import Text as TextType
from sqlalchemy.types import TypeDecorator, UserDefinedType


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


def json_type() -> JSON:
    return JSON()


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{float(value):.8f}" for value in values) + "]"


class PgVector(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "vector"


class VectorValue(TypeDecorator[str | list[float] | None]):
    impl = TextType
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PgVector())
        return dialect.type_descriptor(TextType())

    def process_bind_param(self, value: str | list[float] | None, dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return vector_literal(value)


class TelegramMessage(Base):
    __tablename__ = "telegram_messages"
    __table_args__ = (
        UniqueConstraint("telegram_message_id", "chat_id", name="uq_telegram_message"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    grouped_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    forwarded_from: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_post_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_metadata_json: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict)
    import_status: Mapped[str] = mapped_column(String(32), default="imported", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    media_items: Mapped[list["MediaItem"]] = relationship(
        back_populates="telegram_message", cascade="all, delete-orphan"
    )


class MediaItem(Base):
    __tablename__ = "media_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_message_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    media_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    original_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    preview_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    hover_preview_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    derivative_hashes_json: Mapped[dict[str, str] | None] = mapped_column(json_type(), nullable=True)
    perceptual_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    duplicate_group_id: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    processing_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    public_status: Mapped[str] = mapped_column(String(32), default="published", index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    origin: Mapped[str] = mapped_column(String(32), default="telegram", index=True)
    visibility: Mapped[str] = mapped_column(String(32), default="public", index=True)
    public_original_allowed: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    telegram_message: Mapped[TelegramMessage] = relationship(back_populates="media_items")
    keyframes: Mapped[list["MediaKeyframe"]] = relationship(
        back_populates="media_item", cascade="all, delete-orphan"
    )
    classifications: Mapped[list["Classification"]] = relationship(
        back_populates="media_item", cascade="all, delete-orphan"
    )
    embeddings: Mapped[list["Embedding"]] = relationship(
        back_populates="media_item", cascade="all, delete-orphan"
    )
    curation: Mapped["Curation | None"] = relationship(
        back_populates="media_item", cascade="all, delete-orphan"
    )
    session_curation: Mapped[list["AnonymousSessionCuration"]] = relationship(
        back_populates="media_item", cascade="all, delete-orphan"
    )
    public_tags: Mapped[list["MediaTag"]] = relationship(
        back_populates="media_item", cascade="all, delete-orphan"
    )
    search_document: Mapped["PublicSearchDocument | None"] = relationship(
        back_populates="media_item", cascade="all, delete-orphan"
    )
    public_share: Mapped["PublicMediaShare | None"] = relationship(
        back_populates="media_item", cascade="all, delete-orphan", uselist=False
    )


class SourceIdentity(Base):
    __tablename__ = "source_identities"
    __table_args__ = (
        UniqueConstraint("kind", "namespace", "external_key", name="uq_source_identity"),
        CheckConstraint("kind = 'folder'", name="ck_source_kind"),
        CheckConstraint("length(namespace) > 0 AND length(namespace) <= 128", name="ck_source_namespace"),
        CheckConstraint("length(external_key) > 0 AND length(external_key) <= 1024", name="ck_source_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    external_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("anonymous_sessions.id", ondelete="RESTRICT"))
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="RESTRICT"), nullable=False, unique=True
    )


class MediaKeyframe(Base):
    __tablename__ = "media_keyframes"
    __table_args__ = (
        UniqueConstraint("media_item_id", "frame_index", name="uq_media_keyframe_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    frame_index: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    media_item: Mapped[MediaItem] = relationship(back_populates="keyframes")


class Classification(Base):
    __tablename__ = "classifications"
    __table_args__ = (
        UniqueConstraint("media_item_id", "model", "prompt_version", name="uq_classification_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    compatibility_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    visible_text: Mapped[str] = mapped_column(Text, default="")
    narration: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    meme_format: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    tone: Mapped[list[str]] = mapped_column(json_type(), default=list)
    reaction_type: Mapped[list[str]] = mapped_column(json_type(), default=list)
    topics_json: Mapped[list[str]] = mapped_column(json_type(), default=list)
    keywords_json: Mapped[list[str]] = mapped_column(json_type(), default=list)
    people_or_characters_json: Mapped[list[str]] = mapped_column(json_type(), default=list)
    safety_flags_json: Mapped[list[str]] = mapped_column(json_type(), default=list)
    search_phrases_json: Mapped[list[str]] = mapped_column(json_type(), default=list)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_response_json: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    media_item: Mapped[MediaItem] = relationship(back_populates="classifications")


class Embedding(Base):
    __tablename__ = "embeddings"
    __table_args__ = (
        UniqueConstraint("media_item_id", "embedding_model", "embedding_source", name="uq_embedding"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    embedding_source: Mapped[str] = mapped_column(String(64), nullable=False, default="classification")
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_text_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding_json: Mapped[list[float]] = mapped_column(json_type(), default=list)
    embedding_vector: Mapped[str | list[float] | None] = mapped_column(VectorValue(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    media_item: Mapped[MediaItem] = relationship(back_populates="embeddings")


class Curation(Base):
    __tablename__ = "curation"
    __table_args__ = (UniqueConstraint("media_item_id", name="uq_curation_media_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    manual_tags_json: Mapped[list[str]] = mapped_column(json_type(), default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    media_item: Mapped[MediaItem] = relationship(back_populates="curation")


class AnonymousSession(Base):
    __tablename__ = "anonymous_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ip_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    user_agent_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, index=True
    )

    curation: Mapped[list["AnonymousSessionCuration"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    remix_jobs: Mapped[list["RemixJobRecord"]] = relationship(back_populates="session")


class AnonymousSessionCuration(Base):
    __tablename__ = "anonymous_session_curation"
    __table_args__ = (
        UniqueConstraint("session_id", "media_item_id", name="uq_session_media_curation"),
        Index("ix_session_curation_session_updated", "session_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("anonymous_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    manual_tags_json: Mapped[list[str]] = mapped_column(json_type(), default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    session: Mapped[AnonymousSession] = relationship(back_populates="curation")
    media_item: Mapped[MediaItem] = relationship(back_populates="session_curation")


class MediaTag(Base):
    __tablename__ = "media_tags"
    __table_args__ = (
        UniqueConstraint("media_item_id", "tag_type", "value", "source", name="uq_media_tag"),
        Index("ix_media_tags_lookup", "tag_type", "value", "media_item_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_type: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="classification")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    media_item: Mapped[MediaItem] = relationship(back_populates="public_tags")


class PublicSearchDocument(Base):
    __tablename__ = "public_search_documents"
    __table_args__ = (UniqueConstraint("media_item_id", name="uq_public_search_document_media"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_text: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, index=True
    )

    media_item: Mapped[MediaItem] = relationship(back_populates="search_document")


class PublicShareCode(Base):
    __tablename__ = "public_share_codes"
    __table_args__ = (UniqueConstraint("token_id", "card", name="uq_public_share_code_variant"),)

    code: Mapped[str] = mapped_column(String(5), primary_key=True)
    # Retain retired codes after deletion so an old URL can never point to a different item.
    token_id: Mapped[str] = mapped_column(String(64), nullable=False)
    card: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class PublicMediaShare(Base):
    __tablename__ = "public_media_shares"
    __table_args__ = (
        UniqueConstraint("media_item_id", name="uq_public_media_share_media"),
        UniqueConstraint("token_id", name="uq_public_media_share_token"),
        Index("ix_public_media_shares_active_updated", "active", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_id: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    recipe_grant_json: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    media_item: Mapped[MediaItem] = relationship(back_populates="public_share")


class RemixJobRecord(Base):
    __tablename__ = "remix_jobs"
    __table_args__ = (
        Index("ix_remix_jobs_session_created", "session_id", "created_at"),
        Index("ix_remix_jobs_status_updated", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("anonymous_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    left_id: Mapped[int] = mapped_column(ForeignKey("media_items.id", ondelete="RESTRICT"), nullable=False)
    right_id: Mapped[int] = mapped_column(ForeignKey("media_items.id", ondelete="RESTRICT"), nullable=False)
    safety_mode: Mapped[str] = mapped_column(String(32), default="standard")
    tweaks_json: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    message: Mapped[str] = mapped_column(Text, default="Queued")
    retry_after_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    block_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict)
    execution_capture_json: Mapped[dict[str, Any] | None] = mapped_column(json_type(), nullable=True)
    execution_first_captured_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    saved_media_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    embedding_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    session: Mapped[AnonymousSession | None] = relationship(back_populates="remix_jobs")
    attempts: Mapped[list["RemixAttemptRecord"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    sources: Mapped[list["RemixSourceRecord"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class RemixAttemptRecord(Base):
    __tablename__ = "remix_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt", name="uq_remix_attempt_number"),
        Index("ix_remix_attempts_job_created", "job_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("remix_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    mitigation_id: Mapped[str] = mapped_column(String(128), nullable=False, default="original")
    label: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    block_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    job: Mapped[RemixJobRecord] = relationship(back_populates="attempts")


class RemixSourceRecord(Base):
    __tablename__ = "remix_sources"
    __table_args__ = (
        UniqueConstraint("remix_media_item_id", "source_media_item_id", name="uq_remix_source_link"),
        Index("ix_remix_sources_source", "source_media_item_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("remix_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    remix_media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    job: Mapped[RemixJobRecord | None] = relationship(back_populates="sources")


class GeminiCallError(Base):
    __tablename__ = "gemini_call_errors"
    __table_args__ = (
        Index("ix_gemini_call_errors_created", "created_at"),
        Index("ix_gemini_call_errors_model_method_created", "model", "method", "created_at"),
        Index("ix_gemini_call_errors_status_created", "status_code", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, default="")
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    will_retry: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    error_type: Mapped[str] = mapped_column(String(128), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    raw_body: Mapped[str] = mapped_column(Text, default="")
    retry_after_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    quota_metric: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_json: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict)
    response_json: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AiProviderCircuit(Base):
    __tablename__ = "ai_provider_circuits"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), default="closed", index=True)
    reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    probe_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    probe_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ImportCheckpoint(Base):
    __tablename__ = "import_checkpoints"
    __table_args__ = (UniqueConstraint("source", "chat_id", name="uq_import_checkpoint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="telegram_saved")
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False)
    last_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ImportFailure(Base):
    __tablename__ = "import_failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    stage: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
