"""Slice A contracts; actual request/identity integration is deliberately staged."""

from dataclasses import replace
import hashlib
import json

import pytest

from meme_arsenal.config import Settings, load_settings
from meme_arsenal import remix_policy
from meme_arsenal.remix_execution import (
    RemixExecutionCapture, RemixGenerationProvenance, capture_execution,
)
from meme_arsenal.style_guidance import render_style_guidance, style_guidance_digest


@pytest.mark.parametrize("name", ["classification_style_guidance", "remix_style_guidance"])
def test_normalization(name):
    for raw, expected in [(None, ""), (" \r\na\rb\r\n\t猫\u200d🙂 \t", "a\nb\n\t猫\u200d🙂"),
                          ("🙂" * 1000, "🙂" * 1000), (" \t", "")]:
        assert getattr(Settings(**{name: raw}), name) == expected
        assert getattr(replace(Settings(), **{name: raw}), name) == expected
    assert getattr(Settings(**{name: "e\u0301"}), name) != "é"
    for raw in [False, 12, [], {}, "x" * 1001, "\x00secret", "secret\x7f", "\ud800", "\x85"]:
        with pytest.raises(ValueError) as error:
            Settings(**{name: raw})
        assert "secret" not in str(error.value)
        assert name in str(error.value)


@pytest.mark.parametrize("name", ["classification_style_guidance", "remix_style_guidance"])
def test_layers(tmp_path, monkeypatch, name):
    monkeypatch.chdir(tmp_path)
    env = name.upper()
    monkeypatch.delenv(env, raising=False)
    monkeypatch.delenv(env + "_FILE", raising=False)
    (tmp_path / "config.yaml").write_text(f"{name}: lower\n")
    (tmp_path / "env").write_text(f"{env}=middle\n")
    (tmp_path / ".env").write_text(f"{env}\n")
    assert getattr(load_settings(), name) == "middle"
    selected = tmp_path / "guidance"
    selected.write_text("")
    monkeypatch.setenv(env + "_FILE", str(selected))
    assert getattr(load_settings(), name) == ""
    selected.write_text("file style")
    assert getattr(load_settings(), name) == "file style"
    monkeypatch.setenv(env, "")
    assert getattr(load_settings(), name) == ""
    monkeypatch.delenv(env)
    monkeypatch.delenv(env + "_FILE")
    (tmp_path / ".env").write_text(f"{env}=\n{env}_FILE={selected}\n")
    assert getattr(load_settings(), name) == ""
    (tmp_path / ".env").unlink()
    (tmp_path / "env").unlink()
    (tmp_path / "config.yaml").write_text(f"{name}: null\n")
    assert getattr(load_settings(), name) == ""
    (tmp_path / "config.yaml").write_text(f"{name}: false\n")
    with pytest.raises(ValueError):
        load_settings()
    selected.write_text("\x00")
    monkeypatch.setenv(env + "_FILE", str(selected))
    with pytest.raises(ValueError):
        load_settings()


def capture(style=""):
    return capture_execution(Settings(remix_provider="google", remix_style_guidance=style), "new_job")


def test_wrapper_contract():
    assert render_style_guidance("") == ""
    value = '猫\n"quoted"'
    assert render_style_guidance(value).endswith(json.dumps(value, ensure_ascii=False))
    assert "subordinate" in render_style_guidance(value)


def test_default_exact_and_retained_overlay(monkeypatch):
    default = capture()
    # Captured independently from accepted main 40557355fbf1faaadd7c747029d84906a0b21953.
    assert remix_policy.policy_digest() == "b4f41ce51cd2e4c542c18b7874fbb125be324d6edca3ae8eb970b3657e40a091"
    assert default.payload() == {
        "format": "remix-execution-v1", "origin": "new_job", "provider": "google",
        "model": "models/gemini-3.1-flash-image", "aspect_ratio": "1:1", "image_size": "1K",
        "include_original_video": True, "inline_max_mb": 20.0, "max_video_keyframes": 8,
        "policy_digest": remix_policy.policy_digest(), "algorithm_version": "remix-generation-1",
    }
    current = Settings(remix_style_guidance="B", classification_style_guidance="current C",
                       google_api_key="synthetic-current", embedding_provider="openai")
    assert default.apply(current).remix_style_guidance == ""
    retained = capture(" A\r\n猫 ")
    assert retained.remix_style_guidance == "A\n猫"
    assert retained.format == "remix-execution-v2"
    assert retained.policy_digest != default.policy_digest
    applied = retained.apply(current)
    assert applied.remix_style_guidance == "A\n猫"
    assert applied.classification_style_guidance == "current C"
    assert applied.google_api_key == current.google_api_key
    assert applied.embedding_provider == current.embedding_provider
    assert RemixExecutionCapture.parse(retained.payload()) == retained
    assert style_guidance_digest("A") != style_guidance_digest("B")
    monkeypatch.setattr(remix_policy, "MAIN_PROMPT", "incompatible")
    for selected in (default, retained):
        assert RemixExecutionCapture.parse(selected.payload()) == selected
        with pytest.raises(ValueError, match="create a new job"):
            selected.require_current_policy()


@pytest.mark.parametrize("style", ["", "A"])
def test_strict_shapes(style):
    raw = capture(style).payload()
    for field in raw:
        malformed = dict(raw)
        del malformed[field]
        with pytest.raises(ValueError):
            RemixExecutionCapture.parse(malformed)
    with pytest.raises(ValueError):
        RemixExecutionCapture.parse({**raw, "extra": "x"})
    if not style:
        with pytest.raises(ValueError):
            RemixExecutionCapture.parse({**raw, "remix_style_guidance": ""})
    else:
        for value in (None, "", " A", "A\r\nB", 1, "\x00", "x" * 1001, "\ud800"):
            with pytest.raises(ValueError):
                RemixExecutionCapture.parse({**raw, "remix_style_guidance": value})


def test_historical_provenance_payload_and_authority_hash():
    historical = replace(capture(), algorithm_version="historical", policy_digest="a" * 64)
    raw = {"capture": historical.payload(), "submitted_text_digest": "b" * 64,
           "attachment_mode": "stills", "mitigation_id": None,
           "metadata_origin": "model_json", "attempt_number": 1}
    parsed = RemixGenerationProvenance.parse(raw)
    assert parsed.payload() == raw
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    assert digest(parsed.payload()) == digest(raw)
    assert "remix_style_guidance" not in parsed.payload()["capture"]
