from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ai import classification_block_reason, classification_status
from .config import Settings
from .link_preview import message_link_preview
from .media import media_item_has_original_file, stored_media_file_exists
from .models import Classification, Curation, MediaItem, TelegramMessage


CATALOG_VERSION = 1
CHUNK_SIZE = 250


def catalog_saved_at(media_item: MediaItem, message: TelegramMessage | None) -> datetime:
    """Use the user's save time, not the time historical media was imported."""
    saved_at = message.date if message else None
    if media_item.origin in {"dashboard_upload", "remix"}:
        saved_at = media_item.published_at or saved_at
    saved_at = saved_at or media_item.created_at
    if saved_at.tzinfo is None:
        saved_at = saved_at.replace(tzinfo=timezone.utc)
    return saved_at.astimezone(timezone.utc)


LANGUAGE_LABELS = {
    "cn": "Mandarin Chinese",
    "eng": "English",
    "en": "English",
    "english": "English",
    "he": "Hebrew",
    "heb": "Hebrew",
    "hebrew": "Hebrew",
    "it": "Italian",
    "ita": "Italian",
    "italian": "Italian",
    "ja": "Japanese",
    "japanese": "Japanese",
    "jp": "Japanese",
    "ko": "Korean",
    "kor": "Korean",
    "korean": "Korean",
    "mandarin": "Mandarin Chinese",
    "mandarin chinese": "Mandarin Chinese",
    "portuguese": "Portuguese",
    "pt": "Portuguese",
    "por": "Portuguese",
    "unknown": "Unknown",
    "zh": "Mandarin Chinese",
}


def _text(value: Any) -> str:
    return str(value) if value is not None else ""


def _normalize_language(value: str | None) -> str | None:
    if not value:
        return None
    parts = [
        part.strip()
        for part in value.replace("/", ",").replace("&", ",").split(",")
        if part.strip()
    ]
    if not parts:
        return None
    labels: list[str] = []
    for part in parts:
        key = " ".join(part.lower().split())
        label = LANGUAGE_LABELS.get(key) or part.strip().title()
        if label not in labels:
            labels.append(label)
    return ", ".join(labels)


def _dashboard_relative(settings: Settings, stored_path: str | None) -> str | None:
    if not stored_path:
        return None
    if not stored_media_file_exists(settings, stored_path):
        return None
    absolute = settings.reserve_path / stored_path
    return os.path.relpath(absolute, settings.dashboard_dist_dir).replace(os.sep, "/")


def _hover_preview_path(settings: Settings, media_item: MediaItem) -> str | None:
    if media_item.hover_preview_path:
        return _dashboard_relative(settings, media_item.hover_preview_path)
    if media_item.media_type != "video":
        return None
    candidate = settings.video_previews_dir / f"media-{media_item.id:08d}.mp4"
    if candidate.exists() and candidate.stat().st_size > 0:
        return os.path.relpath(candidate, settings.dashboard_dist_dir).replace(os.sep, "/")
    return None


def _latest_classification(
    classifications: dict[int, list[Classification]],
    media_item_id: int,
) -> Classification | None:
    rows = classifications.get(media_item_id) or []
    if not rows:
        return None
    return max(rows, key=lambda row: (
        row.model == "manual" and row.prompt_version == "manual-v1"
        and (row.raw_response_json or {}).get("provider") == "manual",
        row.created_at, row.id,
    ))


def _item_payload(
    settings: Settings,
    media_item: MediaItem,
    message: TelegramMessage,
    classification: Classification | None,
    curation: Curation | None,
    remix_sources: list[dict[str, Any]] | None = None,
    remix_used_by: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    link_preview = message_link_preview(message)
    return {
        "id": media_item.id,
        "telegram_message_id": message.telegram_message_id,
        "chat_id": message.chat_id,
        "telegram_post_url": message.telegram_post_url,
        "date": message.date.isoformat() if message.date else None,
        "saved_at": catalog_saved_at(media_item, message).isoformat(),
        "added_at": (
            media_item.created_at.replace(tzinfo=timezone.utc)
            if media_item.created_at.tzinfo is None else media_item.created_at
        ).isoformat(),
        "caption": message.caption or "",
        "source_url": link_preview.get("url") or link_preview.get("display_url"),
        "source_display_url": link_preview.get("display_url"),
        "source_site": link_preview.get("site_name"),
        "source_title": link_preview.get("title"),
        "source_description": link_preview.get("description"),
        "source_embed_url": link_preview.get("embed_url"),
        "link_preview": link_preview,
        "grouped_id": message.grouped_id,
        "forwarded_from": message.forwarded_from,
        "media_type": media_item.media_type,
        "mime_type": media_item.mime_type,
        "thumbnail_path": _dashboard_relative(settings, media_item.thumbnail_path),
        "preview_path": _dashboard_relative(settings, media_item.preview_path),
        "hover_preview_path": _hover_preview_path(settings, media_item),
        "original_path": _dashboard_relative(settings, media_item.original_path),
        "duration_seconds": media_item.duration_seconds,
        "width": media_item.width,
        "height": media_item.height,
        "size_bytes": media_item.size_bytes,
        "sha256": media_item.sha256,
        "perceptual_hash": media_item.perceptual_hash,
        "duplicate_group_id": media_item.duplicate_group_id,
        "processing_status": media_item.processing_status,
        "classification_status": classification_status(classification),
        "classification_block_reason": classification_block_reason(classification),
        "summary": classification.summary if classification else "",
        "visible_text": classification.visible_text if classification else "",
        "narration": classification.narration if classification else "",
        "language": _normalize_language(classification.language) if classification else None,
        "meme_format": classification.meme_format if classification else None,
        "tone": classification.tone if classification else [],
        "reaction_type": classification.reaction_type if classification else [],
        "topics": classification.topics_json if classification else [],
        "keywords": classification.keywords_json if classification else [],
        "people_or_characters": classification.people_or_characters_json if classification else [],
        "safety_flags": classification.safety_flags_json if classification else [],
        "search_phrases": classification.search_phrases_json if classification else [],
        "favorite": bool(curation.favorite) if curation else False,
        "archived": bool(curation.archived) if curation else False,
        "manual_tags": curation.manual_tags_json if curation else [],
        "notes": curation.notes if curation else "",
        "remix_sources": remix_sources or [],
        "remix_used_by": remix_used_by or [],
    }


def _catalog_search_text(item: dict[str, Any]) -> str:
    text_fields = [
        item.get("caption", ""),
        item.get("telegram_post_url", ""),
        item.get("source_url", ""),
        item.get("source_display_url", ""),
        item.get("source_site", ""),
        item.get("source_title", ""),
        item.get("source_description", ""),
        item.get("media_type", ""),
        item.get("mime_type", ""),
        item.get("summary", ""),
        item.get("visible_text", ""),
        item.get("narration", ""),
        item.get("language", ""),
        item.get("meme_format", ""),
        " ".join(item.get("topics") or []),
        " ".join(item.get("keywords") or []),
        " ".join(item.get("tone") or []),
        " ".join(item.get("reaction_type") or []),
        " ".join(item.get("people_or_characters") or []),
        " ".join(item.get("safety_flags") or []),
        " ".join(item.get("manual_tags") or []),
        " ".join(item.get("search_phrases") or []),
        item.get("notes", ""),
        "favorite" if item.get("favorite") else "",
        "archived" if item.get("archived") else "",
    ]
    return "\n".join(_text(field) for field in text_fields).lower()


def _remix_source_ids(message: TelegramMessage) -> list[int]:
    raw_metadata = message.raw_metadata_json or {}
    source_ids = raw_metadata.get("source_media_item_ids")
    if not isinstance(source_ids, list):
        return []
    return [int(source_id) for source_id in source_ids if isinstance(source_id, int) or str(source_id).isdigit()]


def _remix_source_payload(
    settings: Settings,
    media_item: MediaItem,
    message: TelegramMessage,
    classification: Classification | None,
) -> dict[str, Any]:
    title = (
        classification.summary
        if classification and classification.summary
        else message.caption or message_link_preview(message).get("title") or f"Media {media_item.id}"
    )
    return {
        "id": media_item.id,
        "title": title,
        "media_type": media_item.media_type,
        "thumbnail_path": _dashboard_relative(settings, media_item.thumbnail_path),
        "preview_path": _dashboard_relative(settings, media_item.preview_path),
        "original_path": _dashboard_relative(settings, media_item.original_path),
    }


def catalog_item_payload(session: Session, settings: Settings, media_item_id: int) -> dict[str, Any]:
    media_item = session.get(MediaItem, media_item_id)
    if media_item is None:
        raise ValueError(f"Media item {media_item_id} was not found")
    message = session.get(TelegramMessage, media_item.telegram_message_id)
    if message is None:
        raise ValueError(f"Telegram message {media_item.telegram_message_id} was not found")
    classifications: dict[int, list[Classification]] = defaultdict(list)
    for row in session.scalars(select(Classification)).all():
        classifications[row.media_item_id].append(row)
    curation = session.scalar(select(Curation).where(Curation.media_item_id == media_item_id))
    remix_sources: list[dict[str, Any]] = []
    for source_id in _remix_source_ids(message):
        source_item = session.get(MediaItem, source_id)
        if source_item is None:
            continue
        source_message = session.get(TelegramMessage, source_item.telegram_message_id)
        if source_message is None:
            continue
        source_classifications = session.scalars(
            select(Classification).where(Classification.media_item_id == source_item.id)
        ).all()
        remix_sources.append(
            _remix_source_payload(
                settings,
                source_item,
                source_message,
                _latest_classification({source_item.id: source_classifications}, source_item.id),
            )
        )
    remix_used_by: list[dict[str, Any]] = []
    for used_by_item in session.scalars(select(MediaItem).order_by(MediaItem.id.desc())).all():
        used_by_message = session.get(TelegramMessage, used_by_item.telegram_message_id)
        if used_by_message is None or media_item_id not in _remix_source_ids(used_by_message):
            continue
        remix_used_by.append(
            _remix_source_payload(
                settings,
                used_by_item,
                used_by_message,
                _latest_classification(classifications, used_by_item.id),
            )
        )
    return _item_payload(
        settings,
        media_item,
        message,
        _latest_classification(classifications, media_item_id),
        curation,
        remix_sources,
        remix_used_by,
    )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n")


def export_catalog(session: Session, settings: Settings, chunk_size: int = CHUNK_SIZE) -> dict[str, Any]:
    settings.catalog_dir.mkdir(parents=True, exist_ok=True)

    messages = {
        message.id: message
        for message in session.scalars(select(TelegramMessage)).all()
    }
    classifications: dict[int, list[Classification]] = defaultdict(list)
    for row in session.scalars(select(Classification)).all():
        classifications[row.media_item_id].append(row)
    curation = {
        row.media_item_id: row
        for row in session.scalars(select(Curation)).all()
    }

    media_items = session.scalars(select(MediaItem)).all()
    media_items.sort(
        key=lambda item: (catalog_saved_at(item, messages.get(item.telegram_message_id)), item.id),
        reverse=True,
    )
    media_by_id = {media_item.id: media_item for media_item in media_items}
    used_by_ids: dict[int, list[int]] = defaultdict(list)
    for media_item in media_items:
        if not media_item_has_original_file(settings, media_item):
            continue
        message = messages.get(media_item.telegram_message_id)
        if not message:
            continue
        for source_id in _remix_source_ids(message):
            used_by_ids[source_id].append(media_item.id)

    items: list[dict[str, Any]] = []
    for media_item in media_items:
        if not media_item_has_original_file(settings, media_item):
            continue
        message = messages.get(media_item.telegram_message_id)
        if not message:
            continue
        remix_sources = []
        for source_id in _remix_source_ids(message):
            source_item = media_by_id.get(source_id)
            if not source_item or not media_item_has_original_file(settings, source_item):
                continue
            source_message = messages.get(source_item.telegram_message_id)
            if not source_message:
                continue
            remix_sources.append(
                _remix_source_payload(
                    settings,
                    source_item,
                    source_message,
                    _latest_classification(classifications, source_item.id),
                )
            )
        remix_used_by = []
        for used_by_id in used_by_ids.get(media_item.id, []):
            used_by_item = media_by_id.get(used_by_id)
            if not used_by_item or not media_item_has_original_file(settings, used_by_item):
                continue
            used_by_message = messages.get(used_by_item.telegram_message_id)
            if not used_by_message:
                continue
            remix_used_by.append(
                _remix_source_payload(
                    settings,
                    used_by_item,
                    used_by_message,
                    _latest_classification(classifications, used_by_item.id),
                )
            )
        items.append(
            _item_payload(
                settings,
                media_item,
                message,
                _latest_classification(classifications, media_item.id),
                curation.get(media_item.id),
                remix_sources,
                remix_used_by,
            )
        )

    for old_chunk in settings.catalog_dir.glob("items-*.json"):
        old_chunk.unlink()

    chunks: list[str] = []
    for chunk_index, offset in enumerate(range(0, len(items), chunk_size)):
        chunk_name = f"items-{chunk_index:03d}.json"
        _write_json(settings.catalog_dir / chunk_name, {"items": items[offset : offset + chunk_size]})
        chunks.append(f"../../catalog/{chunk_name}")

    tag_counters: dict[str, Counter[str]] = {
        "topics": Counter(),
        "keywords": Counter(),
        "tone": Counter(),
        "reaction_type": Counter(),
        "meme_format": Counter(),
        "media_type": Counter(),
        "language": Counter(),
        "manual_tags": Counter(),
        "duplicate_group": Counter(),
    }
    search_documents: list[dict[str, Any]] = []
    for item in items:
        for field in ["topics", "keywords", "tone", "reaction_type", "manual_tags"]:
            tag_counters[field].update(item.get(field) or [])
        for field, counter_name in [
            ("meme_format", "meme_format"),
            ("media_type", "media_type"),
            ("language", "language"),
            ("duplicate_group_id", "duplicate_group"),
        ]:
            value = item.get(field)
            if value:
                tag_counters[counter_name][str(value)] += 1
        search_documents.append({"id": item["id"], "text": _catalog_search_text(item)})

    tags = {
        key: [{"value": value, "count": count} for value, count in counter.most_common()]
        for key, counter in tag_counters.items()
    }
    index = {
        "version": CATALOG_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "item_count": len(items),
        "chunk_size": chunk_size,
        "chunks": chunks,
        "tags_path": "../../catalog/tags.json",
        "search_index_path": "../../catalog/search-index.json",
        "search_results_base": "../../catalog/search-results",
    }
    _write_json(settings.catalog_dir / "index.json", index)
    _write_json(settings.catalog_dir / "tags.json", tags)
    _write_json(settings.catalog_dir / "search-index.json", {"documents": search_documents})
    return index
