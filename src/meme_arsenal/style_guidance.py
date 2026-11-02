"""Bounded nonsecret operator prose and its explicit versioned rendering contract."""

import hashlib
import json
import unicodedata


STYLE_GUIDANCE_VERSION = "style-guidance-v1"
STYLE_GUIDANCE_WRAPPER = (
    "\n\nOperator style guidance (subordinate to required schema, factual grounding, "
    "safety, mitigation, composition constraints and selected tweaks):\n{0}"
)


def normalize_style_guidance(value, field):
    if value is None:
        return ""
    if type(value) is not str:
        raise ValueError(f"{field}: must be a string")
    if any(
        unicodedata.category(c) == "Cs"
        or (unicodedata.category(c) == "Cc" and c not in "\t\n\r")
        for c in value
    ):
        raise ValueError(f"{field}: invalid control character")
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) > 1000:
        raise ValueError(f"{field}: exceeds 1000 code points")
    return value


def render_style_guidance(value):
    """Render subordinate operator prose; empty guidance preserves the base request."""
    return STYLE_GUIDANCE_WRAPPER.format(json.dumps(value, ensure_ascii=False)) if value else ""


def style_guidance_digest(value):
    return hashlib.sha256(
        json.dumps([STYLE_GUIDANCE_VERSION, value], ensure_ascii=False, separators=(",", ":"))
        .encode("utf-8")
    ).hexdigest()


def extended_remix_policy_digest(base_digest):
    return hashlib.sha256(
        json.dumps(
            [base_digest, STYLE_GUIDANCE_VERSION, STYLE_GUIDANCE_WRAPPER],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
