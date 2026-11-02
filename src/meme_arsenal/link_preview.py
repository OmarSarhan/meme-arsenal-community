from __future__ import annotations

from typing import Any

from .models import TelegramMessage


LINK_PREVIEW_TEXT_FIELDS = (
    "title",
    "description",
    "site_name",
    "display_url",
    "url",
    "embed_url",
    "type",
)


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def extract_link_preview_from_media(media: Any) -> dict[str, Any] | None:
    webpage = getattr(media, "webpage", None)
    if webpage is None:
        return None

    preview: dict[str, Any] = {}
    for field in [
        "url",
        "display_url",
        "site_name",
        "title",
        "description",
        "type",
        "embed_url",
        "embed_type",
        "embed_width",
        "embed_height",
        "duration",
        "author",
    ]:
        value = getattr(webpage, field, None)
        if value is not None:
            preview[field] = _clean_text(value) if isinstance(value, str) else value

    photo = getattr(webpage, "photo", None)
    document = getattr(webpage, "document", None)
    preview["has_preview_media"] = bool(photo or document)
    if document:
        preview["preview_media_kind"] = "document"
    elif photo:
        preview["preview_media_kind"] = "photo"

    return preview if any(value for value in preview.values()) else None


def link_preview_text(preview: dict[str, Any] | None) -> str:
    if not preview:
        return ""
    return "\n".join(
        str(preview[field]).strip()
        for field in LINK_PREVIEW_TEXT_FIELDS
        if field in preview and str(preview[field]).strip()
    )


def message_link_preview(message: TelegramMessage | None) -> dict[str, Any]:
    if message is None:
        return {}
    raw = message.raw_metadata_json or {}
    preview = raw.get("link_preview")
    return preview if isinstance(preview, dict) else {}


def merge_link_preview_metadata(
    raw_metadata: dict[str, Any] | None,
    preview: dict[str, Any] | None,
) -> dict[str, Any]:
    metadata = dict(raw_metadata or {})
    if preview:
        metadata["link_preview"] = preview
    return metadata

