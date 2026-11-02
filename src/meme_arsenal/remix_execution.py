"""Server-owned, non-secret generation identity; parsing never adopts today's policy."""

from dataclasses import asdict, dataclass, fields, replace
import hashlib
import json
import math
import re
import unicodedata

from .config import Settings
from .google_api import google_model_resource
from .remix_policy import ALGORITHM_VERSION, policy_digest
from .style_guidance import extended_remix_policy_digest, normalize_style_guidance


def _text(value, field, limit=128):
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > limit
        or any(unicodedata.category(c).startswith("C") for c in value)
    ):
        raise ValueError(field)


def _hash(value, field):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(field)


def _parse(cls, value):
    if value is None:
        return None
    if type(value) is not dict or set(value) != {f.name for f in fields(cls)}:
        raise ValueError(cls.__name__)
    return cls(**value)


@dataclass(frozen=True)
class RemixExecutionCapture:
    format: str
    origin: str
    provider: str
    model: str
    aspect_ratio: str
    image_size: str
    include_original_video: bool
    inline_max_mb: float
    max_video_keyframes: int
    policy_digest: str
    algorithm_version: str
    remix_style_guidance: str = ""

    def __post_init__(self):
        for field, allowed in (
            ("format", ("remix-execution-v1", "remix-execution-v2")),
            ("origin", ("new_job", "new_direct", "legacy_first_execution")),
            ("provider", ("google",)),
        ):
            if type(getattr(self, field)) is not str or getattr(self, field) not in allowed:
                raise ValueError(field)
        _text(self.model, "model", 256)
        if (
            re.fullmatch(r"(?:models|tunedModels)/[A-Za-z0-9][A-Za-z0-9._-]{0,127}", self.model)
            is None
        ):
            raise ValueError("model")
        for field in ("aspect_ratio", "image_size", "algorithm_version"):
            _text(getattr(self, field), field)
        if type(self.include_original_video) is not bool:
            raise ValueError("include_original_video")
        if (
            type(self.inline_max_mb) not in (int, float)
            or (type(self.inline_max_mb) is float and not math.isfinite(self.inline_max_mb))
            or self.inline_max_mb < 0
        ):
            raise ValueError("inline_max_mb")
        if type(self.max_video_keyframes) is not int or self.max_video_keyframes < 0:
            raise ValueError("max_video_keyframes")
        _hash(self.policy_digest, "policy_digest")
        guidance = self.remix_style_guidance
        if type(guidance) is not str or normalize_style_guidance(guidance, "remix_style_guidance") != guidance:
            raise ValueError("remix_style_guidance")
        if bool(guidance) != (self.format == "remix-execution-v2"):
            raise ValueError("remix_style_guidance")

    @classmethod
    def parse(cls, value):
        if value is None:
            return None
        expected = {f.name for f in fields(cls)}
        if type(value) is dict and value.get("format") != "remix-execution-v2":
            expected.remove("remix_style_guidance")
        if type(value) is not dict or set(value) != expected:
            raise ValueError(cls.__name__)
        return cls(**value)

    def payload(self):
        value = asdict(self)
        if self.format == "remix-execution-v1":
            del value["remix_style_guidance"]
        return value

    def require_current_policy(self):
        expected = policy_digest()
        if self.format == "remix-execution-v2":
            expected = extended_remix_policy_digest(expected)
        if self.algorithm_version != ALGORITHM_VERSION or self.policy_digest != expected:
            raise ValueError("algorithm_version/policy_digest: create a new job")

    def apply(self, current: Settings) -> Settings:
        self.require_current_policy()
        return replace(
            current,
            remix_provider=self.provider,
            remix_model=self.model,
            remix_aspect_ratio=self.aspect_ratio,
            remix_image_size=self.image_size,
            remix_include_video_original=self.include_original_video,
            remix_video_inline_max_mb=self.inline_max_mb,
            max_video_keyframes=self.max_video_keyframes,
            remix_style_guidance=self.remix_style_guidance,
        )


def capture_execution(settings: Settings, origin: str) -> RemixExecutionCapture:
    # Validate before google_model_resource strips whitespace or raises a vendor error.
    _text(settings.remix_model, "model", 256)
    return RemixExecutionCapture(
        "remix-execution-v2" if settings.remix_style_guidance else "remix-execution-v1",
        origin,
        settings.effective_remix_provider,
        google_model_resource(settings.remix_model),
        settings.remix_aspect_ratio,
        settings.remix_image_size,
        settings.remix_include_video_original,
        settings.remix_video_inline_max_mb,
        settings.max_video_keyframes,
        extended_remix_policy_digest(policy_digest()) if settings.remix_style_guidance else policy_digest(),
        ALGORITHM_VERSION,
        settings.remix_style_guidance,
    )


def ordered_text_digest(texts) -> str:
    """Hash all submitted text parts, in order, including labels and retry suffixes."""
    if type(texts) not in (list, tuple) or any(type(t) is not str for t in texts):
        raise ValueError("submitted_texts")
    return hashlib.sha256(
        json.dumps(list(texts), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class RemixGenerationProvenance:
    capture: RemixExecutionCapture
    submitted_text_digest: str
    attachment_mode: str
    mitigation_id: str | None
    metadata_origin: str
    # Existing outer RemixAttemptRecord.attempt (one based), not HTTP retries.
    attempt_number: int | None

    def __post_init__(self):
        if type(self.capture) is not RemixExecutionCapture:
            raise ValueError("capture")
        _hash(self.submitted_text_digest, "submitted_text_digest")
        for field, allowed in (
            ("attachment_mode", ("metadata-only", "stills", "original-video")),
            ("metadata_origin", ("model_json", "local_fallback")),
            (
                "mitigation_id",
                (
                    None,
                    "original",
                    "safer-start",
                    "soften-left",
                    "soften-right",
                    "soften-both",
                    "visual-only",
                    "metadata-only",
                ),
            ),
        ):
            value = getattr(self, field)
            if (value is not None and type(value) is not str) or value not in allowed:
                raise ValueError(field)
        if self.attempt_number is not None and (
            type(self.attempt_number) is not int or self.attempt_number <= 0
        ):
            raise ValueError("attempt_number")

    @classmethod
    def parse(cls, value):
        if type(value) is dict:
            value = dict(value)
            value["capture"] = RemixExecutionCapture.parse(value.get("capture"))
        return _parse(cls, value)

    def payload(self):
        value = asdict(self)
        value["capture"] = self.capture.payload()
        return value
