from __future__ import annotations

from .remix_rejection_request import remix_rejection_request

from . import remix_policy as policy
from .remix_execution import RemixExecutionCapture, RemixGenerationProvenance, capture_execution, ordered_text_digest
from .style_guidance import render_style_guidance
from .result_identity import generation_identity, saved_result_identity

import base64
import json
import mimetypes
import re
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .ai import ClassificationPayload, _gemini_block_reason, classify_media_item
from .branding import APP_SLUG
from .catalog import catalog_item_payload, export_catalog
from .config import Settings
from .embeddings import embed_media_item
from .gemini_media import gemini_inline_image_data
from .google_api import GEMINI_BLOCK_FINISH_REASONS, GeminiApiError, is_unprocessable_input_image_error, post_google_model
from .media import infer_media_type, path_sha256, process_media_item, reserve_absolute, reserve_relative
from .models import Classification, MediaItem, MediaKeyframe, TelegramMessage, utc_now
from . import saved_remix_generation


REMIX_PROMPT_VERSION = "remix-v2"
REMIX_TWEAK_ANALYSIS_PROMPT_VERSION = "remix-tweak-analysis-v2"
REMIX_CHAT_ID = f"{APP_SLUG}-remix"
REMIX_SAFETY_MODE_STANDARD = "standard"
REMIX_SAFETY_MODE_SAFER = "safer"
REMIX_SAFETY_MODES = {REMIX_SAFETY_MODE_STANDARD, REMIX_SAFETY_MODE_SAFER}
MAX_REMIX_TWEAK_CONTROLS = 4
REMIX_TWEAK_ENDPOINT_LABEL_MAX_LENGTH = 256
REMIX_REJECTION_REASON_MAX_LENGTH = 40
MAX_REMIX_MESSAGE_ID_ATTEMPTS = 5
UNSAFE_REMIX_METADATA_RE = re.compile(
    r"\b(9/11|terroris[mt]|terrorist|second tower|twin towers?|morbid|offensive|disturbing|"
    r"dark humor|dark joke|death|dead|die|killed?|attack)\b",
    re.IGNORECASE,
)
REMIX_TWEAK_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "controls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "description": {"type": "string"},
                    "low_label": {"type": "string"},
                    "high_label": {"type": "string"},
                    "default_value": {"type": "number"},
                },
                "required": [
                    "id",
                    "label",
                    "description",
                    "low_label",
                    "high_label",
                    "default_value",
                ],
                "propertyOrdering": [
                    "id",
                    "label",
                    "description",
                    "low_label",
                    "high_label",
                    "default_value",
                ],
            },
        },
    },
    "required": ["summary", "controls"],
    "propertyOrdering": ["summary", "controls"],
}


def _safe_tweak_text(value: Any, max_length: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text or UNSAFE_REMIX_METADATA_RE.search(text):
        return ""
    return text[:max_length].strip()


def _tweak_slug(value: Any) -> str:
    text = _safe_tweak_text(value, 80).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return slug[:48].strip("-")


class RemixTweakControl(BaseModel):
    id: str
    label: str
    description: str = ""
    low_label: str = "less"
    high_label: str = "more"
    default_value: int = Field(default=50, ge=0, le=100)

    @field_validator("id", mode="before")
    @classmethod
    def normalize_id(cls, value: Any) -> str:
        slug = _tweak_slug(value)
        if not slug:
            raise ValueError("tweak control id is required")
        return slug

    @field_validator("label", mode="before")
    @classmethod
    def clean_label(cls, value: Any) -> str:
        text = _safe_tweak_text(value, 256)
        if not text:
            raise ValueError("tweak control label is required")
        return text

    @field_validator("description", mode="before")
    @classmethod
    def clean_description(cls, value: Any) -> str:
        return _safe_tweak_text(value, 2048)

    @field_validator("low_label", mode="before")
    @classmethod
    def clean_low_label(cls, value: Any) -> str:
        return _safe_tweak_text(value, REMIX_TWEAK_ENDPOINT_LABEL_MAX_LENGTH) or "less"

    @field_validator("high_label", mode="before")
    @classmethod
    def clean_high_label(cls, value: Any) -> str:
        return _safe_tweak_text(value, REMIX_TWEAK_ENDPOINT_LABEL_MAX_LENGTH) or "more"

    @field_validator("default_value", mode="before")
    @classmethod
    def clean_default_value(cls, value: Any) -> int:
        return _clamp_tweak_value(value)


class RemixTweakAnalysis(BaseModel):
    prompt_version: str = REMIX_TWEAK_ANALYSIS_PROMPT_VERSION
    summary: str = ""
    controls: list[RemixTweakControl] = Field(default_factory=list)
    source: str = "model"
    notice: str = ""

    @field_validator("prompt_version", mode="before")
    @classmethod
    def fixed_prompt_version(cls, _value: Any) -> str:
        return REMIX_TWEAK_ANALYSIS_PROMPT_VERSION

    @field_validator("summary", mode="before")
    @classmethod
    def clean_summary(cls, value: Any) -> str:
        return _safe_tweak_text(value, 2048)

    @field_validator("source", mode="before")
    @classmethod
    def clean_source(cls, value: Any) -> str:
        return _safe_tweak_text(value, 48) or "model"

    @field_validator("notice", mode="before")
    @classmethod
    def clean_notice(cls, value: Any) -> str:
        return _safe_tweak_text(value, 2048)

    def payload(self) -> dict[str, Any]:
        return {
            "prompt_version": self.prompt_version,
            "summary": self.summary,
            "controls": [control.model_dump() for control in self.controls],
            "source": self.source,
            "notice": self.notice,
        }


class RemixTweaks(BaseModel):
    analysis_prompt_version: str = REMIX_TWEAK_ANALYSIS_PROMPT_VERSION
    summary: str = ""
    controls: list[RemixTweakControl] = Field(default_factory=list)
    values: dict[str, int] = Field(default_factory=dict)
    schizo_level: int = 0
    chronically_online_level: int = 0
    no_text: bool = False
    comic_strip: bool = False
    anti_slop: bool = False

    @field_validator("analysis_prompt_version", mode="before")
    @classmethod
    def clean_analysis_prompt_version(cls, value: Any) -> str:
        return _safe_tweak_text(value, 64) or REMIX_TWEAK_ANALYSIS_PROMPT_VERSION

    @field_validator("summary", mode="before")
    @classmethod
    def clean_summary(cls, value: Any) -> str:
        return _safe_tweak_text(value, 2048)

    def payload(self) -> dict[str, Any]:
        return {
            "analysis_prompt_version": self.analysis_prompt_version,
            "summary": self.summary,
            "controls": [control.model_dump() for control in self.controls],
            "values": self.values,
            "schizo_level": self.schizo_level,
            "chronically_online_level": self.chronically_online_level,
            "no_text": self.no_text,
            "comic_strip": self.comic_strip,
            "anti_slop": self.anti_slop,
        }

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "analysis_prompt_version": self.analysis_prompt_version,
            "summary": self.summary,
            "controls": [
                {
                    **control.model_dump(),
                    "value": self.values.get(control.id, control.default_value),
                }
                for control in self.controls
            ],
        }


def _clamp_tweak_value(value: Any, default: int = 50) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        number = default
    return min(100, max(0, number))


def _clamp_prompt_mode_level(value: Any) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        number = 0
    return min(3, max(0, number))


def _bool_tweak_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _controls_from_raw(raw_controls: Any) -> list[RemixTweakControl]:
    if not isinstance(raw_controls, list):
        return []
    controls: list[RemixTweakControl] = []
    seen_ids: set[str] = set()
    for raw_control in raw_controls:
        try:
            control = RemixTweakControl.model_validate(raw_control)
        except Exception:
            continue
        if control.id in seen_ids:
            continue
        seen_ids.add(control.id)
        controls.append(control)
        if len(controls) >= MAX_REMIX_TWEAK_CONTROLS:
            break
    return controls


def _analysis_from_raw(
    raw: dict[str, Any] | None,
    *,
    source: str = "model",
    notice: str = "",
) -> RemixTweakAnalysis:
    data = raw or {}
    return RemixTweakAnalysis(
        summary=data.get("summary", ""),
        controls=_controls_from_raw(data.get("controls")),
        source=source,
        notice=notice,
    )


def _side_reference_text(text: str, left: RemixSource, right: RemixSource) -> str:
    replacements = (
        (left.media_item.id, "Left"),
        (right.media_item.id, "Right"),
    )
    cleaned = text
    for media_id, side in replacements:
        cleaned = re.sub(
            rf"\b(?:image|media|source|meme)\s*#?\s*{media_id}\b",
            side,
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            rf"\b{media_id}\s*(?:image|media|source|meme)\b",
            side,
            cleaned,
            flags=re.IGNORECASE,
        )
    return cleaned


def _normalize_fallback_side_labels(analysis: RemixTweakAnalysis, left: RemixSource, right: RemixSource) -> RemixTweakAnalysis:
    if analysis.source not in {"conservative", "local_fallback"}:
        return analysis
    controls = [
        control.model_copy(
            update={
                "label": _side_reference_text(control.label, left, right),
                "description": _side_reference_text(control.description, left, right),
                "low_label": _side_reference_text(control.low_label, left, right),
                "high_label": _side_reference_text(control.high_label, left, right),
            }
        )
        for control in analysis.controls
    ]
    return analysis.model_copy(
        update={
            "summary": _side_reference_text(analysis.summary, left, right),
            "controls": controls,
        }
    )


def remix_tweaks_from_payload(raw: Any) -> RemixTweaks | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RemixError("Remix tweaks must be a JSON object")
    controls = _controls_from_raw(raw.get("controls"))
    schizo_level = _clamp_prompt_mode_level(raw.get("schizo_level"))
    chronically_online_level = _clamp_prompt_mode_level(raw.get("chronically_online_level"))
    no_text = _bool_tweak_flag(raw.get("no_text"))
    comic_strip = _bool_tweak_flag(raw.get("comic_strip") or raw.get("as_comic_strip"))
    anti_slop = _bool_tweak_flag(raw.get("anti_slop"))
    if (
        not controls
        and schizo_level <= 0
        and chronically_online_level <= 0
        and not no_text
        and not comic_strip
        and not anti_slop
    ):
        return None

    raw_values = raw.get("values") if isinstance(raw.get("values"), dict) else {}
    values = {
        control.id: _clamp_tweak_value(raw_values.get(control.id), control.default_value)
        for control in controls
    }
    return RemixTweaks(
        analysis_prompt_version=raw.get("analysis_prompt_version") or raw.get("prompt_version"),
        summary=raw.get("summary", ""),
        controls=controls,
        values=values,
        schizo_level=schizo_level,
        chronically_online_level=chronically_online_level,
        no_text=no_text,
        comic_strip=comic_strip,
        anti_slop=anti_slop,
    )


def remix_safety_mode_from_payload(raw: Any) -> str:
    mode = str(raw or REMIX_SAFETY_MODE_STANDARD).strip().lower()
    return mode if mode in REMIX_SAFETY_MODES else REMIX_SAFETY_MODE_STANDARD


class RemixError(RuntimeError):
    pass


class RemixUnavailableError(RemixError):
    pass


class RemixBlockedError(RemixError):
    def __init__(
        self,
        block_reason: str,
        failure_explanation: str | None = None,
        rejection_context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"Gemini blocked remix generation: {block_reason}")
        self.block_reason = block_reason
        self.failure_explanation = failure_explanation
        self.rejection_context = rejection_context or {}


class RemixNoImageError(RemixError):
    def __init__(self, rejection_context: dict[str, Any] | None = None) -> None:
        super().__init__("Gemini response did not include an image")
        self.rejection_context = rejection_context or {}


def _remix_block_context(block_reason: str, response: dict[str, Any] | None = None) -> str:
    details: list[str] = [f"The previous attempt was blocked by Gemini with reason: {block_reason}."]
    if isinstance(response, dict):
        prompt_feedback = response.get("promptFeedback")
        if isinstance(prompt_feedback, dict):
            message = prompt_feedback.get("blockReasonMessage")
            if isinstance(message, str) and message.strip():
                details.append(f"Prompt feedback: {message.strip()}")
            safety_ratings = prompt_feedback.get("safetyRatings")
            if isinstance(safety_ratings, list):
                for rating in safety_ratings:
                    if not isinstance(rating, dict):
                        continue
                    category = rating.get("category")
                    probability = rating.get("probability")
                    blocked = rating.get("blocked")
                    if category:
                        fragments = [str(category)]
                        if probability is not None:
                            fragments.append(f"probability={probability}")
                        if blocked is not None:
                            fragments.append(f"blocked={blocked}")
                        details.append("Safety signal: " + ", ".join(fragments))
        candidates = response.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                finish_reason = candidate.get("finishReason")
                if finish_reason:
                    details.append(f"Candidate finishReason={finish_reason}")
                    break
    if len(details) == 1:
        details.append("Treat this as a safety-related stop and retry with safer source cues.")
    return " ".join(details)


def _concise_rejection_reason(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = re.sub(r"^(?:reason|rejection|blocked)\s*[:\-]\s*", "", text, flags=re.IGNORECASE)
    text = text.strip(" .,:;\"'")
    if not text or text.upper() == "OTHER":
        return ""
    words = text.split()
    if len(words) > 5:
        text = " ".join(words[:5])
    return text[:REMIX_REJECTION_REASON_MAX_LENGTH].strip(" -")


def _humanized_block_reason(block_reason: Any) -> str:
    reason = str(block_reason or "").strip()
    if not reason or reason.upper() == "OTHER":
        return ""
    words = [word.capitalize() for word in re.split(r"[_\s-]+", reason) if word]
    if not words:
        return ""
    if words[-1].lower() in {"safety", "content"}:
        return _concise_rejection_reason(" ".join(words))
    return _concise_rejection_reason(f"{' '.join(words)} safety")


def local_remix_rejection_reason(context: dict[str, Any] | None) -> str:
    context = context or {}
    if context.get("failure_type") == "no_image_returned":
        response = context.get("response")
        if isinstance(response, dict) and response.get("text_excerpt"):
            return "Text-only response"
        return "No image output"
    raw_text = json.dumps(context, ensure_ascii=False).lower()
    if re.search(r"\b(maga|trump|biden|election|politic|snowflake|ideology|red pill|denier)\b", raw_text):
        return "Political mockery"
    if re.search(r"\b(slur|hate|hateful|racis[mt]|antisemit|homophob|transphob|identity)\b", raw_text):
        return "Identity targeting"
    if re.search(r"\b(terror|weapon|gun|blood|gore|death|dead|killed?|attack|disaster|tower)\b", raw_text):
        return "Violence cue"
    if re.search(r"\b(nude|sexual|porn|explicit|fetish)\b", raw_text):
        return "Sexual content"
    if re.search(r"\b(child|minor|underage)\b", raw_text):
        return "Minor safety"
    humanized = _humanized_block_reason(context.get("block_reason"))
    return humanized or "Safety policy"


@dataclass(frozen=True)
class RemixMitigation:
    id: str
    label: str
    description: str
    soften_left: bool = False
    soften_right: bool = False
    visual_only: bool = False
    metadata_only: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "soften_left": self.soften_left,
            "soften_right": self.soften_right,
            "visual_only": self.visual_only,
            "metadata_only": self.metadata_only,
        }


@dataclass(frozen=True)
class RemixSource:
    media_item: MediaItem
    image_paths: tuple[Path, ...]
    metadata: dict[str, Any]
    image_notes: tuple[str, ...] = ()
    video_path: Path | None = None


@dataclass(frozen=True)
class RemixResult:
    job_id: str
    left_id: int
    right_id: int
    image_path: Path
    metadata: ClassificationPayload
    raw_response: dict[str, Any]
    tweaks: RemixTweaks | None = None
    safety_mode: str = REMIX_SAFETY_MODE_STANDARD

    generation_provenance: RemixGenerationProvenance | None = None

    def __post_init__(self):
        if (self.generation_provenance is not None
                and type(self.generation_provenance) is not RemixGenerationProvenance):
            raise ValueError("generation_provenance")

    def payload(self) -> dict[str, Any]:
        return {
            **({"generation_provenance": self.generation_provenance.payload()}
               if self.generation_provenance is not None else {}),
            "generation_identity": generation_identity(
                self.generation_provenance.payload() if self.generation_provenance else None,
            ),
            **self.metadata.model_dump(),
            "image_path": self.image_path.name,
            "safety_mode": self.safety_mode,
        }


def remix_available(settings: Settings) -> bool:
    return remix_unavailable_reason(settings) is None


def remix_unavailable_reason(settings: Settings) -> str | None:
    """Report local configuration readiness without probing credentials or quota."""
    if settings.effective_remix_provider != "google":
        return "Remix requires REMIX_PROVIDER=google (or inherited AI_PROVIDER=google)"
    if not settings.google_api_key or not settings.google_api_key.strip():
        return (
            "Remix requires GEMINI_API_KEY (or GOOGLE_API_KEY / AI_API_KEY); "
            "configure the corresponding *_FILE for community deployment"
        )
    return None


def _latest_classification(session: Session, media_item_id: int) -> Classification | None:
    return session.scalar(
        select(Classification)
        .where(Classification.media_item_id == media_item_id)
        .order_by(Classification.created_at.desc())
    )


def _image_is_readable(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except (OSError, UnidentifiedImageError):
        return False


def _source_image_paths(
    session: Session,
    settings: Settings,
    media_item: MediaItem,
) -> tuple[tuple[Path, str], ...]:
    candidates: list[tuple[Path, str]] = []
    preview = reserve_absolute(settings, media_item.preview_path)
    thumbnail = reserve_absolute(settings, media_item.thumbnail_path)
    if preview and preview.exists():
        candidates.append((preview, policy.PREVIEW_IMAGE))
    elif thumbnail and thumbnail.exists():
        candidates.append((thumbnail, policy.THUMBNAIL_IMAGE))

    if media_item.media_type == "video":
        keyframes = session.scalars(
            select(MediaKeyframe)
            .where(MediaKeyframe.media_item_id == media_item.id)
            .order_by(MediaKeyframe.frame_index)
        ).all()
        for keyframe in keyframes[: settings.max_video_keyframes]:
            path = reserve_absolute(settings, keyframe.path)
            if path and path.exists():
                timestamp = keyframe.timestamp_seconds
                note = (
                    policy.VIDEO_KEYFRAME_AT.format(timestamp)
                    if timestamp is not None
                    else policy.VIDEO_KEYFRAME
                )
                candidates.append((path, note))
    elif not candidates and media_item.media_type in {"image", "gif", "sticker"}:
        path = reserve_absolute(settings, media_item.original_path)
        if path and path.exists():
            candidates.append((path, policy.ORIGINAL_IMAGE))

    unique_paths = list(dict.fromkeys(candidates))
    readable_paths = tuple((path, note) for path, note in unique_paths if _image_is_readable(path))
    if readable_paths:
        return readable_paths
    raise RemixError(f"Media item {media_item.id} does not have a readable image preview")


def _source_metadata(session: Session, settings: Settings, media_item: MediaItem) -> RemixSource:
    message = session.get(TelegramMessage, media_item.telegram_message_id)
    classification = _latest_classification(session, media_item.id)
    metadata = {
        "id": media_item.id,
        "caption": message.caption if message else "",
        "media_type": media_item.media_type,
        "mime_type": media_item.mime_type,
        "summary": classification.summary if classification else "",
        "visible_text": classification.visible_text if classification else "",
        "narration": classification.narration if classification else "",
        "language": classification.language if classification else "",
        "meme_format": classification.meme_format if classification else "",
        "tone": classification.tone if classification else [],
        "reaction_type": classification.reaction_type if classification else [],
        "topics": classification.topics_json if classification else [],
        "keywords": classification.keywords_json if classification else [],
        "people_or_characters": classification.people_or_characters_json if classification else [],
        "search_phrases": classification.search_phrases_json if classification else [],
    }
    image_inputs = _source_image_paths(session, settings, media_item)
    original_path = reserve_absolute(settings, media_item.original_path)
    video_path = (
        original_path
        if media_item.media_type == "video" and original_path and original_path.exists()
        else None
    )
    return RemixSource(
        media_item=media_item,
        image_paths=tuple(path for path, _note in image_inputs),
        image_notes=tuple(note for _path, note in image_inputs),
        video_path=video_path,
        metadata=metadata,
    )


def _inline_image(path: Path) -> dict[str, str]:
    return gemini_inline_image_data(path)


def _inline_media(path: Path) -> dict[str, str]:
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"mime_type": mime_type, "data": data}


def _safe_remix_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if not UNSAFE_REMIX_METADATA_RE.search(str(value))]


def _safe_remix_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text or UNSAFE_REMIX_METADATA_RE.search(text):
        return ""
    return text


def _safe_remix_metadata(source: RemixSource) -> dict[str, Any]:
    metadata = source.metadata
    keywords = _safe_remix_list(metadata.get("keywords"))
    topics = _safe_remix_list(metadata.get("topics"))
    visible_text = _safe_remix_text(metadata.get("visible_text"))
    summary = _safe_remix_text(metadata.get("summary"))
    if not summary:
        descriptors = [*keywords[:3], *topics[:3], _safe_remix_text(metadata.get("meme_format"))]
        descriptors = [descriptor for descriptor in descriptors if descriptor]
        summary = ", ".join(dict.fromkeys(descriptors)) or policy.SOURCE_MEME.format(source.media_item.id)

    return {
        "id": metadata.get("id"),
        "media_type": metadata.get("media_type"),
        "mime_type": metadata.get("mime_type"),
        "summary": summary,
        "visible_text": visible_text[:160],
        "meme_format": _safe_remix_text(metadata.get("meme_format")),
        "tone": _safe_remix_list(metadata.get("tone")),
        "reaction_type": _safe_remix_list(metadata.get("reaction_type")),
        "topics": topics,
        "keywords": keywords,
        "people_or_characters": _safe_remix_list(metadata.get("people_or_characters")),
        "search_phrases": _safe_remix_list(metadata.get("search_phrases")),
    }


def _remix_risk_score(source: RemixSource) -> int:
    values: list[Any] = []
    for value in source.metadata.values():
        if isinstance(value, list):
            values.extend(value)
        else:
            values.append(value)
    return sum(1 for value in values if UNSAFE_REMIX_METADATA_RE.search(str(value or "")))


def _softened_remix_metadata(source: RemixSource, strict: bool = False) -> dict[str, Any]:
    metadata = _safe_remix_metadata(source)
    if not strict:
        return metadata
    keep = {
        "id",
        "media_type",
        "mime_type",
        "summary",
        "meme_format",
        "keywords",
        "people_or_characters",
    }
    softened = {key: value for key, value in metadata.items() if key in keep}
    softened["summary"] = (
        policy.SOFTENED_SOURCE_SUMMARY.format(source.media_item.id, metadata.get('media_type') or 'media')
    )
    softened["keywords"] = list(metadata.get("keywords", []))[:4]
    return softened


def _mitigated_source_context(
    left: RemixSource,
    right: RemixSource,
    mitigation: RemixMitigation | None,
) -> dict[str, Any]:
    if mitigation and mitigation.metadata_only:
        left_metadata = _softened_remix_metadata(left, strict=True)
        right_metadata = _softened_remix_metadata(right, strict=True)
        return {
            "left": {
                **left_metadata,
                "instruction": policy.METADATA_ONLY_INSTRUCTION,
            },
            "right": {
                **right_metadata,
                "instruction": policy.METADATA_ONLY_INSTRUCTION,
            },
        }
    if mitigation and mitigation.visual_only:
        return {
            "left": {
                "id": left.media_item.id,
                "media_type": left.media_item.media_type,
                "instruction": policy.VISUAL_ONLY_INSTRUCTION,
            },
            "right": {
                "id": right.media_item.id,
                "media_type": right.media_item.media_type,
                "instruction": policy.VISUAL_ONLY_INSTRUCTION,
            },
        }
    return {
        "left": _softened_remix_metadata(left, strict=bool(mitigation and mitigation.soften_left)),
        "right": _softened_remix_metadata(right, strict=bool(mitigation and mitigation.soften_right)),
    }


def remix_mitigation_plan(
    left: RemixSource,
    right: RemixSource,
    safety_mode: str = REMIX_SAFETY_MODE_STANDARD,
) -> list[RemixMitigation | None]:
    if safety_mode == REMIX_SAFETY_MODE_SAFER:
        return [
            RemixMitigation(
                id=policy.MITIGATION_SAFER_START,
                label=policy.MITIGATION_SAFER_REQUEST,
                description=policy.MITIGATION_DESCRIPTION_1,
                soften_left=True,
                soften_right=True,
            ),
            RemixMitigation(
                id=policy.MITIGATION_VISUAL_ONLY,
                label=policy.MITIGATION_VISUAL_ONLY_RETRY,
                description=policy.MITIGATION_DESCRIPTION_2,
                visual_only=True,
            ),
            RemixMitigation(
                id=policy.MITIGATION_METADATA_ONLY,
                label=policy.MITIGATION_DESCRIPTION_3,
                description=policy.MITIGATION_DESCRIPTION_4,
                soften_left=True,
                soften_right=True,
                metadata_only=True,
            ),
        ]

    left_risk = _remix_risk_score(left)
    right_risk = _remix_risk_score(right)
    target_left = left_risk >= right_risk
    targeted = RemixMitigation(
        id=policy.MITIGATION_SOFTEN_LEFT if target_left else policy.MITIGATION_SOFTEN_RIGHT,
        label=policy.MITIGATION_TARGETED_SOFT_RETRY,
        description=(
            policy.MITIGATION_DESCRIPTION_5.format(policy.MITIGATION_LEFT if target_left else policy.MITIGATION_RIGHT)
        ),
        soften_left=target_left,
        soften_right=not target_left,
    )
    return [
        None,
        targeted,
        RemixMitigation(
            id=policy.MITIGATION_SOFTEN_BOTH,
            label=policy.MITIGATION_SAFER_RETRY,
            description=policy.MITIGATION_DESCRIPTION_6,
            soften_left=True,
            soften_right=True,
        ),
        RemixMitigation(
            id=policy.MITIGATION_VISUAL_ONLY,
            label=policy.MITIGATION_VISUAL_ONLY_RETRY,
            description=policy.MITIGATION_DESCRIPTION_7,
            visual_only=True,
        ),
        RemixMitigation(
            id=policy.MITIGATION_METADATA_ONLY,
            label=policy.MITIGATION_DESCRIPTION_8,
            description=policy.MITIGATION_DESCRIPTION_9,
            soften_left=True,
            soften_right=True,
            metadata_only=True,
        ),
    ]


def _schizo_prompt_line(level: int) -> str:
    if level <= 0:
        return ""
    if level == 1:
        return policy.SURREAL_LEVEL_1
    if level == 2:
        return policy.SURREAL_LEVEL_2
    return policy.SURREAL_LEVEL_3


def _chronically_online_prompt_line(level: int) -> str:
    if level <= 0:
        return ""
    if level == 1:
        return policy.INTERNET_LEVEL_1
    if level == 2:
        return policy.INTERNET_LEVEL_2
    return policy.INTERNET_LEVEL_3


def _text_quality_instruction_prompt(tweaks: RemixTweaks | None) -> str:
    if tweaks and tweaks.no_text:
        return ""
    return policy.TEXT_QUALITY


def _hard_remix_instruction_prompt(tweaks: RemixTweaks | None) -> str:
    if not tweaks or not (tweaks.no_text or tweaks.comic_strip or tweaks.anti_slop):
        return ""
    instructions: list[str] = []
    if tweaks.no_text:
        instructions.append(
            policy.NO_TEXT
        )
    if tweaks.comic_strip:
        instructions.append(
            policy.COMIC_STRIP
        )
    if tweaks.anti_slop:
        instructions.append(
            policy.ANTI_SLOP
        )
    instruction_text = "\n".join(instructions)
    return (
        policy.COMPOSITION_CONSTRAINTS.format(instruction_text)
    )


def _remix_prompt(
    left: RemixSource,
    right: RemixSource,
    tweaks: RemixTweaks | None = None,
    mitigation: RemixMitigation | None = None,
    failure_context: str | None = None,
    style_guidance: str = "",
) -> str:
    source_context = _mitigated_source_context(left, right, mitigation)
    tweak_context = ""
    if tweaks and tweaks.controls:
        tweak_context = (
            policy.TWEAK_SLIDERS.format(json.dumps(tweaks.prompt_payload(), ensure_ascii=False, indent=2))
        )
    schizo_context = _schizo_prompt_line(tweaks.schizo_level if tweaks else 0)
    chronically_online_context = _chronically_online_prompt_line(
        tweaks.chronically_online_level if tweaks else 0
    )
    text_quality_context = _text_quality_instruction_prompt(tweaks)
    hard_instruction_context = _hard_remix_instruction_prompt(tweaks)
    mitigation_context = ""
    if mitigation:
        mitigation_context = (
            policy.MITIGATION.format(json.dumps(mitigation.payload(), ensure_ascii=False, indent=2))
        )
    failure_context_section = ""
    if failure_context:
        failure_context_section = (
            policy.FAILURE_CONTEXT.format(failure_context)
        )
    return policy.MAIN_PROMPT.format(
        json.dumps(source_context, ensure_ascii=False, indent=2),
        tweak_context,
        schizo_context,
        chronically_online_context,
        text_quality_context,
        hard_instruction_context,
        mitigation_context,
        failure_context_section,
    ) + render_style_guidance(style_guidance)


def _remix_rejection_context(
    left: RemixSource,
    right: RemixSource,
    *,
    block_reason: str | None = None,
    response: dict[str, Any] | None,
    tweaks: RemixTweaks | None = None,
    mitigation: RemixMitigation | None = None,
    failure_type: str = "blocked",
    response_text: str = "",
    parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    response_context: dict[str, Any] = {}
    if isinstance(response, dict):
        prompt_feedback = response.get("promptFeedback")
        if isinstance(prompt_feedback, dict):
            response_context["prompt_feedback"] = {
                key: prompt_feedback.get(key)
                for key in ("blockReason", "blockReasonMessage", "safetyRatings")
                if key in prompt_feedback
            }
        candidates = response.get("candidates")
        if isinstance(candidates, list):
            finish_reasons = [
                candidate.get("finishReason")
                for candidate in candidates
                if isinstance(candidate, dict) and candidate.get("finishReason")
            ]
            if finish_reasons:
                response_context["finish_reasons"] = finish_reasons[:3]
    if parts is not None:
        part_types: list[str] = []
        for part in parts:
            if "text" in part:
                part_types.append("text")
            elif "inline_data" in part or "inlineData" in part:
                part_types.append("inline_data")
            else:
                part_types.append("unknown")
        response_context["part_types"] = part_types[:8]
    if response_text:
        response_context["text_excerpt"] = response_text[:600]

    context = {
        "failure_type": failure_type,
        "mitigation": mitigation.payload() if mitigation else {"id": "original", "label": "Original request"},
        "source_metadata": _mitigated_source_context(left, right, mitigation),
        "tweaks": tweaks.payload() if tweaks else None,
        "response": response_context,
    }
    if block_reason:
        context["block_reason"] = block_reason
    return context


def _should_query_remote_rejection_reason(settings: Settings) -> bool:
    api_key = (settings.google_api_key or "").strip().lower()
    if not api_key or api_key.startswith(("test-", "fake-", "dummy-")):
        return False
    return remix_available(settings)


def explain_remix_rejection(settings: Settings, context: dict[str, Any] | None) -> str:
    context = context or {}
    fallback = local_remix_rejection_reason(context)
    if not _should_query_remote_rejection_reason(settings):
        return fallback
    try:
        response = post_google_model(
            settings,
            settings.remix_model,
            "generateContent",
            remix_rejection_request(context),
        )
    except Exception:
        return fallback

    if _gemini_block_reason(response):
        return fallback
    text = "\n".join(str(part.get("text", "")) for part in _response_parts(response) if part.get("text")).strip()
    raw = _json_from_text(text) if text else None
    reason = _concise_rejection_reason(raw.get("reason") if isinstance(raw, dict) else text)
    return reason or fallback


def _remix_tweak_analysis_prompt(left: RemixSource, right: RemixSource) -> str:
    source_context = {
        "left": _safe_remix_metadata(left),
        "right": _safe_remix_metadata(right),
    }
    return (
        "Analyze these two source memes and propose up to four pair-specific slider controls "
        "that a user can adjust before generating a remix. Return only concise JSON. "
        "Each slider should be a creative tradeoff that matters for this specific pair, not a "
        "generic model setting. Use safe, playful, non-graphic concepts. Do not include "
        "real-world harm, disasters, weapons, or violent events. "
        "Use this exact shape: summary, controls. Each control must include id, label, "
        "description, low_label, high_label, default_value. IDs must be lowercase slugs, "
        "and default_value must be a number from 0 to 100. "
        "Write for a small phone screen: summary is one complete sentence, at most 24 words; "
        "labels are 1-3 short words (at most 28 characters); endpoint labels are 1-2 words "
        "(at most 18 characters); descriptions are one complete sentence of at most 14 words. "
        "Use plain language, not long source titles, jargon, tag lists, or repeated explanations. "
        "Rewrite shorter instead of ending with ellipses or cutting off a sentence.\n\n"
        f"Source metadata:\n{json.dumps(source_context, ensure_ascii=False, indent=2)}"
    )


def _conservative_tweak_metadata(source: RemixSource, side: str) -> dict[str, Any]:
    metadata = _safe_remix_metadata(source)
    return {
        "side": side,
        "media_type": source.media_item.media_type,
        "mime_type": source.media_item.mime_type,
        "meme_format": metadata.get("meme_format") or "meme",
        "tone": list(metadata.get("tone", []))[:3],
        "reaction_type": list(metadata.get("reaction_type", []))[:3],
        "visual_count": len(source.image_paths),
    }


def _conservative_remix_tweak_analysis_prompt(left: RemixSource, right: RemixSource) -> str:
    source_context = {
        "left": _conservative_tweak_metadata(left, "Left"),
        "right": _conservative_tweak_metadata(right, "Right"),
    }
    return (
        "Propose safe, conservative remix slider controls for two meme sources using only "
        "abstract format, tone, and media-type cues. Do not quote or infer source captions, "
        "political claims, profanity, allegations, real-world harm, weapons, disasters, or "
        "sensitive events. Keep the sliders generic enough to be safe but still useful for "
        "a remix. Return only concise JSON with this exact shape: summary, controls. Each "
        "control must include id, label, description, low_label, high_label, default_value. "
        "IDs must be lowercase slugs, and default_value must be a number from 0 to 100. "
        "Do not refer to media ids, image ids, or numbered images in labels. If a slider "
        "compares the sources, use Left and Right as the endpoint names. "
        "Write for a small phone screen: summary is one complete sentence of at most 24 words, "
        "labels are 1-3 short words (at most 28 characters), endpoints are 1-2 words "
        "(at most 18 characters), and descriptions are complete sentences of at most 14 words. "
        "Use plain language without tag lists or repeated explanations. Rewrite shorter "
        "rather than cutting off text or adding ellipses.\n\n"
        f"Conservative source cues:\n{json.dumps(source_context, ensure_ascii=False, indent=2)}"
    )


def _fallback_tweak_analysis(left: RemixSource, right: RemixSource, notice: str) -> RemixTweakAnalysis:
    return RemixTweakAnalysis(
        source="local_fallback",
        notice=notice,
        summary="Using safe general remix controls for this pair.",
        controls=_controls_from_raw(
            [
                {
                    "id": "source-balance",
                    "label": "Source balance",
                    "description": "Which selected source leads the composition.",
                    "low_label": "left-led",
                    "high_label": "right-led",
                    "default_value": 50,
                },
                {
                    "id": "text-weight",
                    "label": "Text weight",
                    "description": "How much the remix relies on caption-style wording.",
                    "low_label": "visual",
                    "high_label": "caption-led",
                    "default_value": 45,
                },
                {
                    "id": "energy",
                    "label": "Energy",
                    "description": "How busy and intense the final image feels.",
                    "low_label": "clean",
                    "high_label": "busy",
                    "default_value": 50,
                },
                {
                    "id": "twist-level",
                    "label": "Twist level",
                    "description": "How far the remix departs from the source framing.",
                    "low_label": "faithful",
                    "high_label": "absurd",
                    "default_value": 55,
                },
            ]
        ),
    )


def _gemini_tweak_failure_notice(exc: GeminiApiError) -> str:
    if exc.status == "RESOURCE_EXHAUSTED":
        return "Gemini is cooling down, so safe default tweaks are shown for now."
    if exc.status in GEMINI_BLOCK_FINISH_REASONS or "blocked response" in str(exc).lower():
        return "Gemini blocked detailed analysis for this pair, so safe default tweaks are shown instead."
    return "Gemini could not prepare custom tweaks, so safe defaults are shown for now."


def _source_visual_parts(label: str, source: RemixSource) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [
        {
            "text": (
                policy.VISUALS_LABEL.format(label, source.media_item.media_type, len(source.image_paths))
            )
        }
    ]
    for index, image_path in enumerate(source.image_paths, start=1):
        note = source.image_notes[index - 1] if index - 1 < len(source.image_notes) else policy.VISUAL_SAMPLE
        parts.append({"text": policy.VISUAL_LABEL.format(label, index, note)})
        parts.append({"inline_data": _inline_image(image_path)})
    return parts


def _source_video_parts(
    settings: Settings,
    label: str,
    source: RemixSource,
    mitigation: RemixMitigation | None = None,
) -> list[dict[str, Any]]:
    if mitigation is not None or not settings.remix_include_video_original:
        return []
    video_path = source.video_path
    if not video_path or not video_path.exists():
        return []
    if video_path.stat().st_size > settings.remix_video_inline_max_mb * 1024 * 1024:
        return []
    return [
        {
            "text": (
                policy.VIDEO_LABEL.format(label)
            )
        },
        {"inline_data": _inline_media(video_path)},
    ]


def _json_from_text(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        loaded = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            loaded = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return None
    return loaded if isinstance(loaded, dict) else None


def _fallback_metadata(left: RemixSource, right: RemixSource) -> ClassificationPayload:
    left_metadata = _safe_remix_metadata(left)
    right_metadata = _safe_remix_metadata(right)
    left_summary = left_metadata.get("summary") or f"meme {left.media_item.id}"
    right_summary = right_metadata.get("summary") or f"meme {right.media_item.id}"
    topics = [*left_metadata.get("topics", []), *right_metadata.get("topics", [])]
    keywords = [*left_metadata.get("keywords", []), *right_metadata.get("keywords", [])]
    reaction_type = [*left_metadata.get("reaction_type", []), *right_metadata.get("reaction_type", [])]
    keyword_summary = ", ".join(dict.fromkeys(str(value) for value in keywords if value))[:220]
    if not keyword_summary:
        keyword_summary = f"source {left.media_item.id} and {right.media_item.id}"
    return ClassificationPayload(
        summary=f"Generated meme using {keyword_summary}"[:240],
        visible_text="",
        narration=f"A generated remix combining: {left_summary}; {right_summary}",
        language="unknown",
        meme_format="generated remix meme",
        tone=list(dict.fromkeys([*left_metadata.get("tone", []), *right_metadata.get("tone", [])])),
        reaction_type=list(dict.fromkeys(reaction_type)),
        topics=list(dict.fromkeys(topics))[:8],
        keywords=list(dict.fromkeys([*keywords, "remix", "generated meme"]))[:12],
        people_or_characters=list(
            dict.fromkeys(
                [
                    *left_metadata.get("people_or_characters", []),
                    *right_metadata.get("people_or_characters", []),
                ]
            )
        ),
        safety_flags=[],
        search_phrases=list(
            dict.fromkeys(
                [
                    "generated remix meme",
                    str(left_summary)[:120],
                    str(right_summary)[:120],
                    *left_metadata.get("search_phrases", [])[:2],
                    *right_metadata.get("search_phrases", [])[:2],
                ]
            )
        )[:6],
        confidence=0.55,
    )


def _remix_caption(summary: str) -> str:
    text = (summary or "").strip()
    lowered = text.lower()
    if (
        lowered.startswith("remix of ")
        or lowered.startswith("a remix of ")
        or lowered.startswith("an remix of ")
        or lowered.startswith("the remix of ")
    ):
        return "Generated remix"
    return text or "Generated remix"


def _remix_classification_raw_metadata(
    result: RemixResult,
    source_media_item_ids: list[int],
    remix_tweaks: dict[str, Any] | None,
    base_payload: dict[str, Any] | None = None,
    *,
    include_response: bool = True,
) -> dict[str, Any]:
    payload = dict(base_payload or {})
    payload.update(
        {
            "source": "remix",
            "remix_job_id": result.job_id,
            "source_media_item_ids": source_media_item_ids,
            "safety_mode": result.safety_mode,
            "generation": {
                "provenance": result.generation_provenance.payload() if result.generation_provenance else None,
                **({"response": result.raw_response} if include_response else {}),
            },
        }
    )
    if remix_tweaks:
        payload["remix_tweaks"] = remix_tweaks
    return payload


def _parse_response_metadata(text: str, left: RemixSource, right: RemixSource) -> ClassificationPayload:
    return _parse_response_metadata_with_origin(text, left, right)[0]


def _parse_response_metadata_with_origin(
    text: str, left: RemixSource, right: RemixSource,
) -> tuple[ClassificationPayload, str]:
    data = _json_from_text(text)
    if data is None:
        return _fallback_metadata(left, right), "local_fallback"
    try:
        return ClassificationPayload.model_validate(data), "model_json"
    except Exception:
        return _fallback_metadata(left, right), "local_fallback"


def _request_provenance(
    capture: RemixExecutionCapture,
    parts: list[dict[str, Any]],
    mitigation: RemixMitigation | None,
    attempt_number: int | None,
    *,
    original_video_attached: bool = False,
) -> RemixGenerationProvenance:
    media = [part["inline_data"] for part in parts if "inline_data" in part]
    mode = "metadata-only"
    if media:
        mode = "original-video" if original_video_attached else "stills"
    return RemixGenerationProvenance(
        capture=capture,
        submitted_text_digest=ordered_text_digest([part["text"] for part in parts if "text" in part]),
        attachment_mode=mode,
        mitigation_id=mitigation.id if mitigation else "original",
        metadata_origin="local_fallback",
        attempt_number=attempt_number,
    )


def _response_parts(response: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return []
    first_candidate = candidates[0] if isinstance(candidates[0], dict) else {}
    content = first_candidate.get("content") if isinstance(first_candidate, dict) else {}
    parts = content.get("parts") if isinstance(content, dict) else []
    if not isinstance(parts, list):
        return []
    return [part for part in parts if isinstance(part, dict)]


def _image_data_from_parts(parts: list[dict[str, Any]]) -> tuple[str, bytes] | None:
    for part in parts:
        inline_data = part.get("inline_data") or part.get("inlineData")
        if not isinstance(inline_data, dict):
            continue
        mime_type = str(inline_data.get("mime_type") or inline_data.get("mimeType") or "image/png")
        data = inline_data.get("data")
        if isinstance(data, str):
            return mime_type, base64.b64decode(data)
    return None


def _write_png_image(image_bytes: bytes, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(BytesIO(image_bytes)) as image:
        image.convert("RGB").save(target, "PNG", optimize=True)


def _request_remix_tweak_analysis(
    settings: Settings,
    left: RemixSource,
    right: RemixSource,
    *,
    prompt: str,
    source: str,
    notice: str = "",
    include_visuals: bool = True,
) -> RemixTweakAnalysis:
    parts: list[dict[str, Any]] = [{"text": prompt}]
    if include_visuals:
        parts.extend(_source_visual_parts("Left", left))
        parts.extend(_source_visual_parts("Right", right))

    try:
        response = post_google_model(
            settings,
            settings.remix_model,
            "generateContent",
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": parts,
                    }
                ],
                "generationConfig": {
                    "responseFormat": {
                        "text": {
                            "mimeType": "APPLICATION_JSON",
                            "schema": REMIX_TWEAK_ANALYSIS_SCHEMA,
                        }
                    },
                },
            },
        )
    except GeminiApiError as exc:
        if include_visuals and is_unprocessable_input_image_error(exc):
            raise RemixError("Gemini could not process one source image") from exc
        raise

    block_reason = _gemini_block_reason(response)
    if block_reason:
        raise RemixBlockedError(block_reason)

    response_parts = _response_parts(response)
    text = "\n".join(str(part.get("text", "")) for part in response_parts if part.get("text")).strip()
    analysis = _analysis_from_raw(_json_from_text(text) if text else None, source=source, notice=notice)
    return _normalize_fallback_side_labels(analysis, left, right)


def analyze_remix_tweaks(
    session: Session,
    settings: Settings,
    left_id: int,
    right_id: int,
) -> RemixTweakAnalysis:
    if reason := remix_unavailable_reason(settings):
        raise RemixUnavailableError(reason)
    if left_id == right_id:
        raise RemixError("Choose two different memes to remix")

    left_item = session.get(MediaItem, left_id)
    right_item = session.get(MediaItem, right_id)
    if left_item is None or right_item is None:
        raise RemixError("One or both source memes were not found")

    left = _source_metadata(session, settings, left_item)
    right = _source_metadata(session, settings, right_item)
    try:
        return _request_remix_tweak_analysis(
            settings,
            left,
            right,
            prompt=_remix_tweak_analysis_prompt(left, right),
            source="model",
            include_visuals=True,
        )
    except RemixBlockedError:
        notice = "Gemini blocked detailed analysis for this pair, so conservative safe tweak suggestions are shown instead."
    except RemixError as exc:
        notice = f"{exc}. Conservative text-only tweak suggestions are shown instead."
    except GeminiApiError as exc:
        return _fallback_tweak_analysis(left, right, _gemini_tweak_failure_notice(exc))

    try:
        conservative = _request_remix_tweak_analysis(
            settings,
            left,
            right,
            prompt=_conservative_remix_tweak_analysis_prompt(left, right),
            source="conservative",
            notice=notice,
            include_visuals=False,
        )
        if conservative.controls:
            return conservative
        return _fallback_tweak_analysis(
            left,
            right,
            f"{notice} The conservative response had no sliders, so safe defaults are shown.",
        )
    except Exception:  # noqa: BLE001 - tweak analysis should degrade to local controls.
        return _fallback_tweak_analysis(
            left,
            right,
            f"{notice} The conservative retry also failed, so safe defaults are shown.",
        )


def build_remix_mitigation_plan(
    session: Session,
    settings: Settings,
    left_id: int,
    right_id: int,
    safety_mode: str = REMIX_SAFETY_MODE_STANDARD,
) -> list[RemixMitigation | None]:
    left_item = session.get(MediaItem, left_id)
    right_item = session.get(MediaItem, right_id)
    if left_item is None or right_item is None:
        raise RemixError("One or both source memes were not found")
    left = _source_metadata(session, settings, left_item)
    right = _source_metadata(session, settings, right_item)
    return remix_mitigation_plan(left, right, safety_mode=remix_safety_mode_from_payload(safety_mode))


def generate_remix(
    session: Session,
    settings: Settings,
    left_id: int,
    right_id: int,
    job_id: str | None = None,
    status_callback: Callable[[str, str], None] | None = None,
    tweaks: RemixTweaks | None = None,
    mitigation: RemixMitigation | None = None,
    failure_context: str | None = None,
    safety_mode: str = REMIX_SAFETY_MODE_STANDARD,
    *,
    execution_capture: RemixExecutionCapture | None = None,
    # One-based outer attempt seam for successful-request provenance.
    attempt_number: int | None = None,
) -> RemixResult:
    try:
        capture = execution_capture
        if capture is None:
            if reason := remix_unavailable_reason(settings):
                raise RemixUnavailableError(reason)
            capture = capture_execution(settings, "new_direct")
        if type(capture) is not RemixExecutionCapture:
            raise ValueError("capture")
        settings = capture.apply(settings)
    except ValueError:
        raise RemixUnavailableError("Remix configuration is incompatible. Create a new job.") from None
    if reason := remix_unavailable_reason(settings):
        raise RemixUnavailableError(reason)
    if left_id == right_id:
        raise RemixError("Choose two different memes to remix")

    # Validate caller evidence before any paid request or draft creation.
    _request_provenance(capture, [], mitigation, attempt_number)
    job_id = job_id or uuid.uuid4().hex
    safety_mode = remix_safety_mode_from_payload(safety_mode)
    status_callback = status_callback or (lambda _status, _message: None)
    status_callback("preparing_media", "Preparing source memes")

    left_item = session.get(MediaItem, left_id)
    right_item = session.get(MediaItem, right_id)
    if left_item is None or right_item is None:
        raise RemixError("One or both source memes were not found")

    left = _source_metadata(session, settings, left_item)
    right = _source_metadata(session, settings, right_item)

    generation_config = {
        "responseModalities": ["TEXT", "IMAGE"],
        "imageConfig": {
            "aspectRatio": settings.remix_aspect_ratio,
            "imageSize": settings.remix_image_size,
        },
    }
    left_video_parts = [] if mitigation and mitigation.metadata_only else _source_video_parts(
        settings, policy.LEFT_LABEL, left, mitigation,
    )
    right_video_parts = [] if mitigation and mitigation.metadata_only else _source_video_parts(
        settings, policy.RIGHT_LABEL, right, mitigation,
    )
    generation_parts = [
        {"text": _remix_prompt(left, right, tweaks, mitigation, failure_context, settings.remix_style_guidance)},
        *([] if mitigation and mitigation.metadata_only else _source_visual_parts(policy.LEFT_LABEL, left)),
        *left_video_parts,
        *([] if mitigation and mitigation.metadata_only else _source_visual_parts(policy.RIGHT_LABEL, right)),
        *right_video_parts,
    ]

    provenance = _request_provenance(
        capture, generation_parts, mitigation, attempt_number,
        original_video_attached=bool(left_video_parts or right_video_parts),
    )
    status_callback("calling_gemini", "Calling Gemini image remixer")
    try:
        response = post_google_model(
            settings,
            settings.remix_model,
            "generateContent",
            {
                "contents": [{"role": "user", "parts": generation_parts}],
                "generationConfig": generation_config,
            },
        )
    except GeminiApiError as exc:
        if not is_unprocessable_input_image_error(exc):
            raise
        status_callback("calling_gemini", "Gemini rejected a source image; retrying with metadata only")
        retry_parts = [{"text": policy.METADATA_RETRY.format(
            _remix_prompt(left, right, tweaks, mitigation, failure_context, settings.remix_style_guidance)
        )}]
        provenance = _request_provenance(capture, retry_parts, mitigation, attempt_number)
        response = post_google_model(
            settings,
            settings.remix_model,
            "generateContent",
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": retry_parts,
                    }
                ],
                "generationConfig": generation_config,
            },
        )
        response = {
            **response,
            "media_retry_without_inline_data": True,
            "media_retry_reason": str(exc),
        }

    block_reason = _gemini_block_reason(response)
    if block_reason:
        raise RemixBlockedError(
            block_reason,
            failure_explanation=_remix_block_context(block_reason, response),
            rejection_context=_remix_rejection_context(
                left,
                right,
                block_reason=block_reason,
                response=response,
                tweaks=tweaks,
                mitigation=mitigation,
            ),
        )

    parts = _response_parts(response)
    text = "\n".join(str(part.get("text", "")) for part in parts if part.get("text")).strip()
    image_data = _image_data_from_parts(parts)
    if image_data is None:
        raise RemixNoImageError(
            _remix_rejection_context(
                left,
                right,
                response=response,
                tweaks=tweaks,
                mitigation=mitigation,
                failure_type="no_image_returned",
                response_text=text,
                parts=parts,
            )
        )

    _mime_type, image_bytes = image_data
    draft_path = settings.remix_drafts_dir / f"{job_id}.png"
    _write_png_image(image_bytes, draft_path)
    metadata, metadata_origin = _parse_response_metadata_with_origin(text, left, right)
    has_tweaks = bool(
        tweaks
        and (
            tweaks.controls
            or tweaks.schizo_level
            or tweaks.chronically_online_level
            or tweaks.no_text
            or tweaks.comic_strip
            or tweaks.anti_slop
        )
    )
    return RemixResult(
        job_id=job_id,
        left_id=left_id,
        right_id=right_id,
        image_path=draft_path,
        metadata=metadata,
        raw_response=response,
        generation_provenance=replace(provenance, metadata_origin=metadata_origin),
        tweaks=tweaks if has_tweaks else None,
        safety_mode=safety_mode,
    )


def _next_remix_message_id(session: Session, *, seed: int | None = None) -> int:
    base = seed if seed is not None else -int(time.time() * 1_000_000)
    candidate = base
    while session.scalar(
        select(TelegramMessage.id).where(
            TelegramMessage.chat_id == REMIX_CHAT_ID,
            TelegramMessage.telegram_message_id == candidate,
        )
    ):
        candidate -= 1
    return candidate


def save_remix_result(
    session: Session,
    settings: Settings,
    result: RemixResult,
    *,
    embed: bool = True,
    export_static_catalog: bool = True,
) -> dict[str, Any]:
    if not result.image_path.exists():
        raise RemixError("Generated remix draft image is missing")

    original_dir = settings.originals_dir / "remixes"
    original_dir.mkdir(parents=True, exist_ok=True)
    original_path = original_dir / f"{result.job_id}.png"
    shutil.copy2(result.image_path, original_path)

    metadata = result.metadata
    remix_tweaks = result.tweaks.payload() if result.tweaks else None
    source_media_item_ids = [result.left_id, result.right_id]
    ingredients = []
    for side, source_id in zip(("A", "B"), source_media_item_ids):
        source_classification = _latest_classification(session, source_id)
        if source_classification:
            ingredients.append({
                "side": side,
                "summary": (source_classification.summary or "")[:500],
                "visible_text": (source_classification.visible_text or "")[:300],
            })
    remix_context = {
        "ingredients": ingredients,
        "intent": (result.tweaks.summary if result.tweaks else "")[:600],
        "settings": [
            {
                "label": control.label[:80],
                "value": result.tweaks.values.get(control.id, control.default_value),
                "low": control.low_label[:80],
                "high": control.high_label[:80],
            }
            for control in (result.tweaks.controls[:MAX_REMIX_TWEAK_CONTROLS] if result.tweaks else [])
        ],
        "instructions": {
            key: remix_tweaks[key]
            for key in ("no_text", "comic_strip", "anti_slop", "schizo_level", "chronically_online_level")
        } if remix_tweaks else {},
        "generator_description": (metadata.summary or "")[:500],
    }
    message: TelegramMessage | None = None
    last_insert_error: IntegrityError | None = None
    for attempt in range(MAX_REMIX_MESSAGE_ID_ATTEMPTS):
        # begin_nested autoflushes: keep caller state outside the retry handler
        # and do not add the candidate until the SAVEPOINT exists.
        session.flush()
        candidate = _next_remix_message_id(
            session, seed=-int(time.time() * 1_000_000) - attempt,
        )
        connection = session.connection()
        if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
            # SQLite's legacy driver does not BEGIN for reads or SAVEPOINT.
            # Without an outer transaction, releasing it would commit the row
            # before the caller can roll back a later processing failure.
            connection.exec_driver_sql("BEGIN")
        nested = session.begin_nested()
        try:
            with nested:
                message = TelegramMessage(
                    telegram_message_id=candidate,
                    chat_id=REMIX_CHAT_ID,
                    date=datetime.now(timezone.utc),
                    caption=_remix_caption(metadata.summary),
                    grouped_id=None,
                    forwarded_from=None,
                    raw_metadata_json=_remix_classification_raw_metadata(
                        result, source_media_item_ids, remix_tweaks,
                        {"remix_context": remix_context}, include_response=False,
                    ),
                    import_status="imported",
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
                session.add(message)
                session.flush()
            break
        except IntegrityError as exc:
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            sqlite_conflict = "UNIQUE constraint failed: telegram_messages.telegram_message_id, telegram_messages.chat_id"
            if constraint != "uq_telegram_message" and sqlite_conflict not in str(exc.orig):
                raise
            last_insert_error = exc
            if attempt + 1 >= MAX_REMIX_MESSAGE_ID_ATTEMPTS:
                raise RemixError(
                    "Failed to create a remix record due to a database id conflict. Please retry."
                ) from exc

    if message is None:
        raise RemixError("Failed to create a remix message record.") from last_insert_error

    media_item = MediaItem(
        telegram_message_id=message.id,
        media_type=infer_media_type(original_path, "image/png"),
        mime_type="image/png",
        original_path=reserve_relative(settings, original_path),
        processing_status="pending",
        size_bytes=original_path.stat().st_size,
        sha256=path_sha256(original_path),
    )
    session.add(media_item)
    session.flush()
    process_media_item(session, settings, media_item)

    try:
        classification = classify_media_item(session, settings, media_item, force=True)
    except Exception:
        classification = None

    if classification is None:
        classification = Classification(
            media_item_id=media_item.id,
            model=saved_remix_generation.MODEL,
            prompt_version=saved_remix_generation.FORMAT,
            summary=metadata.summary,
            visible_text=metadata.visible_text,
            narration=metadata.narration,
            language=metadata.language,
            meme_format=metadata.meme_format,
            tone=metadata.tone,
            reaction_type=metadata.reaction_type,
            topics_json=metadata.topics,
            keywords_json=metadata.keywords,
            people_or_characters_json=metadata.people_or_characters,
            safety_flags_json=metadata.safety_flags,
            search_phrases_json=metadata.search_phrases,
            confidence=metadata.confidence,
            raw_response_json=_remix_classification_raw_metadata(
                result=result,
                source_media_item_ids=source_media_item_ids,
                remix_tweaks=remix_tweaks,
                base_payload={"provider": saved_remix_generation.PROVIDER, **metadata.model_dump()},
            ),
        )
        session.add(classification)
        session.flush()
        if result.generation_provenance and result.generation_provenance.metadata_origin == "model_json":
            classification.raw_response_json = {
                **classification.raw_response_json,
                saved_remix_generation.PROVIDER: saved_remix_generation.build_saved_generation_evidence(
                    media_item, classification, result.generation_provenance,
                ),
            }
    else:
        classification.raw_response_json = _remix_classification_raw_metadata(
            result=result,
            source_media_item_ids=source_media_item_ids,
            remix_tweaks=remix_tweaks,
            base_payload=classification.raw_response_json,
        )
    session.flush()

    embedding_error: str | None = None
    if embed:
        try:
            embed_media_item(session, settings, media_item, force=True, classification=classification)
        except Exception as exc:  # noqa: BLE001 - saving the remix should survive embedding quota failures.
            embedding_error = str(exc)

    if export_static_catalog:
        export_catalog(session, settings)
    return {
        "item": {**catalog_item_payload(session, settings, media_item.id),
                 "execution_identity": saved_result_identity(classification, message.raw_metadata_json)},
        "embedding_error": embedding_error,
    }
