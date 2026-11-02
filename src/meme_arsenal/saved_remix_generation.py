"""Pure integrity evidence for metadata returned with a saved generated original."""

import hashlib
import json
import re

from .models import Classification, MediaItem
from .remix_execution import RemixGenerationProvenance

MODEL = "saved-remix-generation"
FORMAT = "saved-remix-generation-v1"
PROVIDER = "saved_remix_generation"
SLOT = (MODEL, FORMAT)
_FIELDS = (
    "summary", "visible_text", "narration", "language", "meme_format", "tone",
    "reaction_type", "topics_json", "keywords_json", "people_or_characters_json",
    "safety_flags_json", "search_phrases_json", "confidence",
)


def _digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _facts(item, row, provenance):
    if (type(item.id) is not int or item.id <= 0 or row.media_item_id != item.id
            or (row.model, row.prompt_version) != SLOT
            or row.execution_provider is not None or row.compatibility_key is not None
            or type(row.raw_response_json) is not dict
            or row.raw_response_json.get("provider") != PROVIDER
            or type(item.sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", item.sha256) is None
            or type(provenance) is not RemixGenerationProvenance
            or provenance.metadata_origin != "model_json"):
        raise ValueError("saved generation evidence")
    return {
        "format": FORMAT,
        "media_item_id": item.id,
        "output_sha256": item.sha256,
        "metadata_sha256": _digest({name: getattr(row, name) for name in _FIELDS}),
        "provenance": provenance.payload(),
    }


def build_saved_generation_evidence(
    item: MediaItem, row: Classification, provenance: RemixGenerationProvenance,
) -> dict:
    """Build from a normalized row; caller persists under the raw provider envelope."""
    facts = _facts(item, row, provenance)
    key = _digest({"domain": "saved-remix-generation-authority-v1", "model": MODEL,
                   "prompt_version": FORMAT, "provider": PROVIDER, **facts})
    return {**facts, "compatibility_key": key}


def is_saved_generation_authoritative(item: MediaItem, row: Classification) -> bool:
    """Validate stored facts without current policy, I/O, settings, or mutation."""
    try:
        raw = row.raw_response_json
        if type(raw) is not dict:
            return False
        evidence = raw.get(PROVIDER)
        if (type(evidence) is not dict or type(evidence.get("media_item_id")) is not int
                or set(evidence) != {"format", "media_item_id", "output_sha256",
                                     "metadata_sha256", "provenance", "compatibility_key"}):
            return False
        provenance = RemixGenerationProvenance.parse(evidence["provenance"])
        return evidence == build_saved_generation_evidence(item, row, provenance)
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
        return False
