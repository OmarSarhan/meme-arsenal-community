from dataclasses import fields, replace
import json
from pathlib import Path

import pytest

from meme_arsenal.config import Settings
from meme_arsenal.remix import RemixResult
from meme_arsenal.ai import ClassificationPayload
from meme_arsenal.remix_execution import (
    RemixExecutionCapture,
    RemixGenerationProvenance,
    capture_execution,
    ordered_text_digest,
)
from meme_arsenal import remix_policy
from meme_arsenal import remix
from meme_arsenal.models import MediaItem


def prompt_cases(module=remix):
    left = module.RemixSource(
        MediaItem(id=1, media_type="image"),
        (),
        {"id": 1, "summary": "playful cat", "keywords": ["cat"]},
    )
    right = module.RemixSource(
        MediaItem(id=2, media_type="video"),
        (),
        {"id": 2, "summary": "dancing dog", "keywords": ["dog"]},
    )
    tweaks = module.RemixTweaks(
        controls=[module.RemixTweakControl(id="energy", label="Energy")],
        values={"energy": 70},
        schizo_level=3,
        chronically_online_level=2,
        comic_strip=True,
        anti_slop=True,
    )
    plan = module.remix_mitigation_plan(left, right)
    for mitigation in plan:
        for selected in (None, tweaks, module.RemixTweaks(no_text=True)):
            yield module._remix_prompt(left, right, selected, mitigation, "retry safely")


def test_prompt_fixtures_unchanged():
    """Expected complete prompt hashes captured at d683cc2 before extraction."""
    expected = json.loads(
        (Path(__file__).parent / "fixtures/remix_generation_prompts.json").read_text()
    )
    assert [ordered_text_digest([text]) for text in prompt_cases()] == expected


def capture(**kwargs):
    return capture_execution(Settings(ai_provider="google", **kwargs), "new_job")


@pytest.mark.parametrize("model", ["example", "models/example", "tunedModels/example"])
def test_capture_roundtrip_and_overlay(model):
    a = capture(
        remix_model=model,
        remix_video_inline_max_mb=0,
        max_video_keyframes=0,
        remix_aspect_ratio="custom ratio",
        remix_image_size="future size",
    )
    assert a.model == (model if "/" in model else "models/" + model)
    assert RemixExecutionCapture.parse(json.loads(json.dumps(a.payload()))) == a
    b = Settings(
        ai_provider="openai",
        google_api_key="dummy-current",
        owner_session_id="owner",
        classification_provider="mock",
        embedding_provider="openai",
    )
    applied = a.apply(b)
    allowed = {
        "remix_provider",
        "remix_model",
        "remix_aspect_ratio",
        "remix_image_size",
        "remix_include_video_original",
        "remix_video_inline_max_mb",
        "max_video_keyframes",
    }
    assert all(
        getattr(applied, f.name) == getattr(b, f.name) for f in fields(b) if f.name not in allowed
    )
    assert applied.effective_remix_provider == "google"
    assert b.remix_provider is None
    assert applied.max_video_keyframes == applied.remix_video_inline_max_mb == 0
    explicit = capture_execution(replace(b, remix_provider="google"), "new_direct")
    assert explicit.provider == "google"


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "models/../secret"),
        ("model", "models/a?key=secret"),
        ("model", "models/a/b"),
        ("model", "models/a\n"),
        ("model", ""),
        ("inline_max_mb", True),
        ("inline_max_mb", float("nan")),
        ("inline_max_mb", float("inf")),
        ("inline_max_mb", -1),
        ("max_video_keyframes", True),
        ("max_video_keyframes", 1.5),
        ("include_original_video", 1),
        ("aspect_ratio", "\x80\x00"),
        ("image_size", "x" * 129),
        ("policy_digest", "SECRET"),
        ("origin", "SECRET"),
        ("format", "future"),
        ("provider", "openai"),
    ],
)
def test_strict_capture(field, value):
    data = capture().payload()
    data[field] = value
    with pytest.raises(ValueError) as exc:
        RemixExecutionCapture.parse(data)
    assert str(exc.value) == field


def test_absent_malformed_and_historical(monkeypatch):
    assert RemixExecutionCapture.parse(None) is None
    for value in ({}, {**capture().payload(), "secret-key": "secret"}, "secret"):
        with pytest.raises(ValueError, match="^RemixExecutionCapture$"):
            RemixExecutionCapture.parse(value)
    old = replace(capture(), policy_digest="a" * 64, algorithm_version="historical")
    assert RemixExecutionCapture.parse(old.payload()) == old
    with pytest.raises(ValueError, match="create a new job"):
        old.apply(Settings())
    original = remix_policy.policy_digest()
    monkeypatch.setattr(remix_policy, "MAIN_PROMPT", "changed", raising=False)
    assert remix_policy.policy_digest() != original
    with pytest.raises(ValueError, match="provider"):
        capture_execution(Settings(), "new_job")


def test_result_evidence():
    evidence = RemixGenerationProvenance(
        capture(), ordered_text_digest(["prompt", "label"]), "stills", None, "model_json", 1
    )
    assert RemixGenerationProvenance.parse(json.loads(json.dumps(evidence.payload()))) == evidence
    historical = replace(evidence, capture=replace(evidence.capture, algorithm_version="old"))
    assert RemixGenerationProvenance.parse(historical.payload()).payload() == historical.payload()
    assert ordered_text_digest(["a", "b"]) != ordered_text_digest(["b", "a"])
    assert ordered_text_digest(["a"]) != ordered_text_digest(["a", "retry"])
    metadata = ClassificationPayload(summary="test")
    result = RemixResult("job", 1, 2, Path("image.png"), metadata, {})
    assert "generation_provenance" not in result.payload()
    assert (
        replace(result, generation_provenance=evidence).payload()["generation_provenance"]
        == evidence.payload()
    )
    for field, value in (
        ("attempt_number", True),
        ("attempt_number", 0),
        ("submitted_text_digest", "secret"),
        ("metadata_origin", "unknown"),
        ("attachment_mode", "unknown"),
        ("capture", None),
    ):
        with pytest.raises(ValueError):
            RemixGenerationProvenance.parse({**evidence.payload(), field: value})
