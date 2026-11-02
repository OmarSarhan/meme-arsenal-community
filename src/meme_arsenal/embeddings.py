from __future__ import annotations

import hashlib
import math
import random

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .ai import classification_decision
from .google_api import google_model_resource, post_google_model
from .link_preview import link_preview_text, message_link_preview
from .models import Classification, Embedding, MediaItem, TelegramMessage, vector_literal
from .saved_remix_generation import SLOT, is_saved_generation_authoritative


def validate_embedding_vector(values: list[float], dimensions: int) -> list[float]:
    if (not isinstance(values, list) or not values or len(values) != dimensions
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in values)):
        raise ValueError("Embedding must contain exactly the requested finite numeric dimensions")
    return values


def cosine_similarity(left: list[float], right: list[float]) -> float:
    validate_embedding_vector(left, len(right))
    validate_embedding_vector(right, len(left))
    length = len(left)
    dot = sum(left[index] * right[index] for index in range(length))
    left_norm = math.sqrt(sum(value * value for value in left[:length]))
    right_norm = math.sqrt(sum(value * value for value in right[:length]))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def deterministic_embedding(text: str, dimensions: int) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    rng = random.Random(int(digest[:16], 16))
    values = [rng.uniform(-1.0, 1.0) for _ in range(dimensions)]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


TEXT_BUILDER_REVISION = "classification-text-v1"


def normalize_embedding_text(text: str) -> str:
    return text.strip() or "saved meme"


def embed_text(settings: Settings, text: str) -> list[float]:
    text = normalize_embedding_text(text)
    provider = settings.effective_embedding_provider
    if provider == "openai":
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else OpenAI()
        response = client.embeddings.create(
            model=settings.embedding_model,
            input=text,
            encoding_format="float",
            dimensions=settings.embedding_dimensions,
        )
        return validate_embedding_vector(list(response.data[0].embedding), settings.embedding_dimensions)
    if provider == "google":
        response = post_google_model(
            settings,
            settings.embedding_model,
            "embedContent",
            {
                "model": google_model_resource(settings.embedding_model),
                "content": {"parts": [{"text": text}]},
                "outputDimensionality": settings.embedding_dimensions,
            },
        )
        values = response.get("embedding", {}).get("values")
        if not isinstance(values, list):
            raise ValueError(f"Gemini embedding response did not include values: {response}")
        return validate_embedding_vector(values, settings.embedding_dimensions)
    if provider == "mock":
        return validate_embedding_vector(deterministic_embedding(text, settings.embedding_dimensions), settings.embedding_dimensions)
    raise ValueError(f"Unsupported embedding provider={provider!r}")


def embedding_vector_literal(settings: Settings, values: list[float]) -> str | None:
    return vector_literal(values)


def embedding_source_text(
    session: Session,
    media_item: MediaItem,
    classification: Classification,
) -> str:
    message = session.get(TelegramMessage, media_item.telegram_message_id)
    parts: list[str] = []
    if message and message.caption:
        parts.append(message.caption)
    preview_text = link_preview_text(message_link_preview(message))
    if preview_text:
        parts.append(preview_text)
    if classification:
        parts.extend(
            [
                classification.summary,
                classification.visible_text,
                classification.narration,
                classification.meme_format or "",
                " ".join(classification.tone or []),
                " ".join(classification.reaction_type or []),
                " ".join(classification.topics_json or []),
                " ".join(classification.keywords_json or []),
                " ".join(classification.search_phrases_json or []),
            ]
        )
    return normalize_embedding_text("\n".join(part for part in parts if part))


def select_embedding_input(session: Session, settings: Settings, media_item: MediaItem,
                           classification: Classification | None = None) -> Classification | None:
    """Pure authority selection: manual, compatible AI, then saved generation.

    Raw remix metadata alone is deliberately not an alternate authority.
    """
    if (settings.ai_model, settings.classification_prompt_version) == ("manual", "manual-v1"):
        raise ValueError("The manual/manual-v1 classification slot is reserved")
    if (settings.ai_model, settings.classification_prompt_version) == SLOT:
        raise ValueError("The saved-remix-generation classification slot is reserved")
    with session.no_autoflush:
        manual = session.scalar(select(Classification).where(
            Classification.media_item_id == media_item.id,
            Classification.model == "manual", Classification.prompt_version == "manual-v1"))
        selected = manual if manual and (manual.raw_response_json or {}).get("provider") == "manual" else None
        if selected is None:
            action, current = classification_decision(session, settings, media_item)
            selected = current if action == "reuse" else None
        if selected is None:
            fallback = session.scalar(select(Classification).where(
                Classification.media_item_id == media_item.id,
                Classification.model == SLOT[0], Classification.prompt_version == SLOT[1]))
            if fallback is not None and is_saved_generation_authoritative(media_item, fallback):
                selected = fallback
    if classification is not None and (selected is None
            or classification.media_item_id != media_item.id or classification.id != selected.id):
        return None
    return selected


def embedding_text_hash(text: str) -> str:
    return hashlib.sha256((TEXT_BUILDER_REVISION + "\0" + normalize_embedding_text(text)).encode()).hexdigest()


def embedding_is_compatible(row: Embedding, settings: Settings, text_hash: str) -> bool:
    if (row.execution_provider != settings.effective_embedding_provider
            or row.embedding_model != settings.embedding_model
            or row.embedding_source != "classification"
            or row.source_text_hash != text_hash
            or row.embedding_dimensions != settings.embedding_dimensions):
        return False
    try:
        validate_embedding_vector(row.embedding_json, settings.embedding_dimensions)
    except ValueError:
        return False
    return True


def embedding_decision(session: Session, settings: Settings, media_item: MediaItem,
                       text_hash: str, force: bool = False) -> tuple[str, Embedding | None]:
    with session.no_autoflush:
        rows = session.scalars(select(Embedding).where(
            Embedding.media_item_id == media_item.id, Embedding.embedding_source == "classification")).all()
    current = next((row for row in rows if row.embedding_model == settings.embedding_model), None)
    if not force:
        if current and embedding_is_compatible(current, settings, text_hash):
            return "reuse", current
        def known(row: Embedding) -> bool:
            return (row.execution_provider in {"mock", "openai", "google"}
                    and isinstance(row.source_text_hash, str) and len(row.source_text_hash) == 64
                    and all(c in "0123456789abcdef" for c in row.source_text_hash))
        if (current and not known(current)) or (rows and not any(known(row) for row in rows)):
            return "unknown", None
    return "execute", current


def embed_media_item(
    session: Session,
    settings: Settings,
    media_item: MediaItem,
    force: bool = False,
    classification: Classification | None = None,
) -> Embedding | None:
    classification = select_embedding_input(session, settings, media_item, classification)
    if not classification:
        return None

    text = embedding_source_text(session, media_item, classification)
    text_hash = embedding_text_hash(text)
    action, existing = embedding_decision(session, settings, media_item, text_hash, force)
    if action != "execute":
        return existing
    values = validate_embedding_vector(embed_text(settings, text), settings.embedding_dimensions)
    literal = embedding_vector_literal(settings, values)
    embedding = existing or Embedding(
        media_item_id=media_item.id,
        embedding_model=settings.embedding_model,
        embedding_source="classification",
        embedding_dimensions=len(values),
    )
    embedding.embedding_dimensions = len(values)
    embedding.embedding_json = values
    embedding.embedding_vector = literal
    embedding.execution_provider = settings.effective_embedding_provider
    embedding.source_text_hash = text_hash
    if existing is None:
        session.add(embedding)
    session.flush()
    return embedding


def embed_pending_media(
    session: Session,
    settings: Settings,
    limit: int | None = None,
    force: bool = False,
) -> dict[str, int | str]:
    statement = (
        select(MediaItem)
        .join(TelegramMessage)
        .order_by(TelegramMessage.date.desc(), TelegramMessage.telegram_message_id.desc(), MediaItem.id.desc())
    )
    counts: dict[str, int | str] = {"embedded": 0, "skipped": 0, "failed": 0}
    attempted = 0
    for media_item in session.scalars(statement).unique().all():
        classification = select_embedding_input(session, settings, media_item)
        if classification is None:
            continue
        text_hash = embedding_text_hash(embedding_source_text(session, media_item, classification))
        if embedding_decision(session, settings, media_item, text_hash, force)[0] != "execute":
            continue
        if limit is not None and attempted >= limit:
            break
        attempted += 1
        try:
            with session.begin_nested():
                if embed_media_item(session, settings, media_item, force=force):
                    counts["embedded"] = int(counts["embedded"]) + 1
                else:
                    counts["skipped"] = int(counts["skipped"]) + 1
        except Exception as exc:
            counts["failed"] = int(counts["failed"]) + 1
            counts["last_error"] = str(exc)
    session.flush()
    return counts
