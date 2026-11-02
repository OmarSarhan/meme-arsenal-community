from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from PIL import Image
import pytest
from sqlalchemy import func, select

from meme_arsenal.ai import ClassificationPayload, classify_pending_media
from meme_arsenal.catalog import catalog_item_payload
from meme_arsenal.db import session_scope
from meme_arsenal.embeddings import embed_pending_media, embed_media_item, embedding_source_text, embedding_text_hash
from meme_arsenal.saved_remix_generation import SLOT, is_saved_generation_authoritative
from meme_arsenal.google_api import GeminiApiError, GeminiRateLimitError
from meme_arsenal.media import process_pending_media, reserve_relative
from meme_arsenal.models import Classification, Embedding, MediaItem, MediaKeyframe, TelegramMessage
from meme_arsenal.remix import (
    REMIX_CHAT_ID,
    REMIX_PROMPT_VERSION,
    REMIX_SAFETY_MODE_SAFER,
    REMIX_TWEAK_ANALYSIS_PROMPT_VERSION,
    RemixNoImageError,
    RemixResult,
    RemixTweakControl,
    _remix_prompt,
    _source_metadata,
    analyze_remix_tweaks,
    build_remix_mitigation_plan,
    explain_remix_rejection,
    generate_remix,
    local_remix_rejection_reason,
    remix_tweaks_from_payload,
    save_remix_result,
)
from meme_arsenal.remix_server import RemixJob, RemixJobStore, _suggested_retry as remix_server_suggested_retry
from meme_arsenal.telegram_importer import import_fixture_messages


def test_remix_tweak_endpoint_labels_keep_parenthetical_examples():
    control = RemixTweakControl.model_validate(
        {
            "id": "stakes",
            "label": "Stakes",
            "description": "How severe the inconvenience feels.",
            "low_label": "Mild Inconvenience (Missed Book Club)",
            "high_label": "Major Drama (Ruined The Big Reveal)",
            "default_value": 44,
        }
    )

    assert control.low_label == "Mild Inconvenience (Missed Book Club)"
    assert control.high_label == "Major Drama (Ruined The Big Reveal)"


def test_remix_rate_limit_retry_suggestion_includes_cooldown():
    explicit = GeminiRateLimitError("quota exhausted", retry_after_seconds=30.2)
    fallback = GeminiRateLimitError("quota exhausted")

    assert remix_server_suggested_retry(explicit, exhausted=False) == "Wait 31s, then retry the same pair."
    assert remix_server_suggested_retry(fallback, exhausted=False) == "Wait 1m, then retry the same pair."


def prepare_archive(settings, fixture_dir):
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)
        process_pending_media(session, settings)
        classify_pending_media(session, settings)
        embed_pending_media(session, settings)


def png_data_url_bytes(color: tuple[int, int, int] = (112, 46, 180)) -> bytes:
    buffer = BytesIO()
    image = Image.new("RGB", (96, 96), color=color)
    image.save(buffer, "PNG")
    return buffer.getvalue()


def gemini_image_response(summary: str = "Deploy smug remix") -> dict:
    metadata = {
        "summary": summary,
        "visible_text": "SHIP IT",
        "narration": "A generated remix combines deployment failure and smug victory energy.",
        "language": "en",
        "meme_format": "generated remix meme",
        "tone": ["playful"],
        "reaction_type": ["victory", "failure"],
        "topics": ["software", "deployment"],
        "keywords": ["deploy", "smug", "remix"],
        "people_or_characters": [],
        "safety_flags": [],
        "search_phrases": ["deploy failure smug remix"],
        "confidence": 0.88,
    }
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": json.dumps(metadata)},
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": base64.b64encode(png_data_url_bytes()).decode("ascii"),
                            }
                        },
                    ]
                }
            }
        ]
    }


def gemini_tweak_analysis_response() -> dict:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(
                                {
                                    "summary": "Lean into deploy failure versus smug victory.",
                                    "controls": [
                                        {
                                            "id": "caption-energy",
                                            "label": "Caption energy",
                                            "description": "How much text carries the joke.",
                                            "low_label": "visual gag",
                                            "high_label": "text-forward",
                                            "default_value": 62,
                                        },
                                        {
                                            "id": "source-balance",
                                            "label": "Source balance",
                                            "description": "Which source drives the composition.",
                                            "low_label": "left-led",
                                            "high_label": "right-led",
                                            "default_value": 48,
                                        },
                                        {
                                            "id": "chaos-level",
                                            "label": "Chaos level",
                                            "description": "How busy the final image feels.",
                                            "low_label": "contained",
                                            "high_label": "frantic",
                                            "default_value": 130,
                                        },
                                        {
                                            "id": "victory-smirk",
                                            "label": "Victory smirk",
                                            "description": "How much smug payoff appears.",
                                            "low_label": "subtle",
                                            "high_label": "obvious",
                                            "default_value": 55,
                                        },
                                        {
                                            "id": "extra-slider",
                                            "label": "Extra slider",
                                            "description": "This should be trimmed.",
                                            "low_label": "less",
                                            "high_label": "more",
                                            "default_value": 50,
                                        },
                                    ],
                                }
                            )
                        }
                    ]
                }
            }
        ]
    }


def gemini_blocked_response() -> dict:
    return {
        "candidates": [
            {
                "finishReason": "IMAGE_SAFETY",
                "content": {"parts": []},
            }
        ]
    }


def test_local_remix_rejection_reason_replaces_generic_other():
    reason = local_remix_rejection_reason(
        {
            "block_reason": "OTHER",
            "source_metadata": {
                "left": {
                    "summary": "Political MAGA snowflake mockery meme",
                    "tone": ["derisive", "political"],
                },
                "right": {"summary": "Red pill choice parody"},
            },
        }
    )

    assert reason == "Political mockery"


def test_local_remix_rejection_reason_handles_no_image_output():
    assert (
        local_remix_rejection_reason(
            {
                "failure_type": "no_image_returned",
                "response": {"text_excerpt": "I can describe the meme, but I cannot generate the image."},
            }
        )
        == "Text-only response"
    )
    assert local_remix_rejection_reason({"failure_type": "no_image_returned", "response": {}}) == "No image output"


def test_explain_remix_rejection_queries_for_concise_reason(monkeypatch, settings):
    captured = {}

    def fake_post(settings, model, method, payload):
        captured["model"] = model
        captured["method"] = method
        captured["payload"] = payload
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "reason": "Political source mockery",
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="unit-google-key",
        remix_model="gemini-3.1-flash-image",
    )

    reason = explain_remix_rejection(
        live_settings,
        {
            "block_reason": "OTHER",
            "source_metadata": {"left": {"summary": "Political meme"}, "right": {"summary": "choice meme"}},
        },
    )

    prompt = captured["payload"]["contents"][0]["parts"][0]["text"]
    assert reason == "Political source mockery"
    assert captured["model"] == "gemini-3.1-flash-image"
    assert captured["method"] == "generateContent"
    assert "Do not return OTHER" in prompt
    assert captured["payload"]["generationConfig"]["responseFormat"]["text"]["schema"]["required"] == ["reason"]


def test_google_remix_payload_includes_both_sources(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
        remix_aspect_ratio="16:9",
        remix_image_size="2K",
    )
    captured = {}

    def fake_post(settings, model, method, payload):
        captured["model"] = model
        captured["method"] = method
        captured["payload"] = json.loads(json.dumps(payload))
        payload["contents"][0]["parts"][0]["text"] = "mutated by transport stub"
        return gemini_image_response()

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)

    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        result = generate_remix(session, live_settings, media_ids[0], media_ids[1], job_id="unit-test")

    parts = captured["payload"]["contents"][0]["parts"]
    inline_images = [part["inline_data"] for part in parts if "inline_data" in part]

    assert result.metadata.summary == "Deploy smug remix"
    assert result.image_path.name == "unit-test.png"
    evidence = result.generation_provenance
    assert evidence.attachment_mode == "stills"
    assert evidence.metadata_origin == "model_json"
    assert evidence.attempt_number is None
    assert evidence.mitigation_id == "original"
    assert evidence.submitted_text_digest == hashlib.sha256(json.dumps(
        [part["text"] for part in parts if "text" in part],
        ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()
    assert captured["model"] == "models/gemini-3.1-flash-image"
    assert captured["method"] == "generateContent"
    generation_config = captured["payload"]["generationConfig"]
    assert generation_config["responseModalities"] == ["TEXT", "IMAGE"]
    assert generation_config["imageConfig"] == {"aspectRatio": "16:9", "imageSize": "2K"}
    assert "responseFormat" not in generation_config
    assert len(inline_images) == 2
    assert {image["mime_type"] for image in inline_images} == {"image/jpeg"}
    assert all(image["data"] for image in inline_images)


@pytest.mark.parametrize("explicit_classification", [False, True])
def test_mixed_classification_and_remix_requests(
    monkeypatch, settings, fixture_dir, explicit_classification,
):
    from types import SimpleNamespace
    from openai.resources.responses import Responses

    settings = replace(
        settings,
        ai_provider="mock" if explicit_classification else "openai",
        classification_provider="openai" if explicit_classification else None,
        remix_provider="google",
        embedding_provider="mock",
        openai_api_key="synthetic-openai-key",
        google_api_key="synthetic-google-key",
        ai_model="explicit-openai-classifier",
        remix_model="explicit-google-remixer",
    )
    classification_requests = []
    remix_requests = []

    def fake_response_create(resource, **request):
        assert resource._client.api_key == "synthetic-openai-key"
        classification_requests.append(request)
        return SimpleNamespace(output_text=ClassificationPayload(
            summary="OpenAI classified fixture",
        ).model_dump_json())

    def fake_google_post(selected, model, method, payload):
        assert selected.google_api_key == "synthetic-google-key"
        remix_requests.append((model, method, payload))
        if "responseModalities" in payload["generationConfig"]:
            return gemini_image_response()
        return gemini_tweak_analysis_response()

    monkeypatch.setattr(Responses, "create", fake_response_create)
    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_google_post)

    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)
        process_pending_media(session, settings)
        report = classify_pending_media(session, settings)
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        assert report["classified"] == len(media_ids) == 2
        assert len(classification_requests) == 2
        assert remix_requests == []
        records = session.scalars(select(Classification)).all()
        assert all(record.raw_response_json["provider"] == "openai" for record in records)
        analysis = analyze_remix_tweaks(session, settings, *media_ids)
        result = generate_remix(session, settings, *media_ids, job_id="mixed-providers")

    assert len(classification_requests) == 2  # Remix never uses the classification adapter.
    for request in classification_requests:
        assert request["model"] == "explicit-openai-classifier"
        assert request["text"]["format"]["name"] == "meme_classification"
        content = request["input"][0]["content"]
        assert content[0]["type"] == "input_text"
        assert any(part["type"] == "input_image" for part in content)
    assert analysis.source == "model"
    assert analysis.summary == "Lean into deploy failure versus smug victory."
    assert result.metadata.summary == "Deploy smug remix"
    assert result.image_path.is_file()
    assert len(remix_requests) == 2
    for model, method, payload in remix_requests:
        assert model == ("models/explicit-google-remixer" if "responseModalities" in payload["generationConfig"] else "explicit-google-remixer")
        assert method == "generateContent"
        parts = payload["contents"][0]["parts"]
        assert len([part for part in parts if "inline_data" in part]) == 2
    assert "responseFormat" in remix_requests[0][2]["generationConfig"]
    assert remix_requests[1][2]["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]


def test_remix_retries_metadata_only_when_gemini_rejects_source_image(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
    )
    calls = []

    def fake_post(settings, model, method, payload):
        calls.append(payload)
        if len(calls) == 1:
            raise GeminiApiError(
                "generateContent failed with HTTP 400: Unable to process input image",
                status_code=400,
                status="INVALID_ARGUMENT",
                raw_body='{"error":{"message":"Unable to process input image.","status":"INVALID_ARGUMENT"}}',
            )
        return gemini_image_response("Metadata fallback remix")

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)

    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        result = generate_remix(session, live_settings, media_ids[0], media_ids[1], job_id="image-retry")

    first_parts = calls[0]["contents"][0]["parts"]
    retry_parts = calls[1]["contents"][0]["parts"]
    assert result.metadata.summary == "Metadata fallback remix"
    assert result.raw_response["media_retry_without_inline_data"] is True
    assert any("inline_data" in part for part in first_parts)
    assert not any("inline_data" in part for part in retry_parts)
    assert "metadata only" in retry_parts[0]["text"]
    evidence = result.generation_provenance
    assert evidence.attachment_mode == "metadata-only"
    assert evidence.submitted_text_digest == hashlib.sha256(json.dumps(
        [part["text"] for part in retry_parts if "text" in part],
        ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()
    assert evidence.submitted_text_digest != hashlib.sha256(json.dumps(
        [part["text"] for part in first_parts if "text" in part],
        ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()


def test_remix_tweak_analysis_returns_sanitized_slider_schema(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
    )
    captured = {}

    def fake_post(settings, model, method, payload):
        captured["model"] = model
        captured["method"] = method
        captured["payload"] = payload
        return gemini_tweak_analysis_response()

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)

    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        analysis = analyze_remix_tweaks(session, live_settings, media_ids[0], media_ids[1])

    parts = captured["payload"]["contents"][0]["parts"]
    inline_images = [part["inline_data"] for part in parts if "inline_data" in part]

    assert analysis.prompt_version == REMIX_TWEAK_ANALYSIS_PROMPT_VERSION
    assert analysis.summary == "Lean into deploy failure versus smug victory."
    assert [control.id for control in analysis.controls] == [
        "caption-energy",
        "source-balance",
        "chaos-level",
        "victory-smirk",
    ]
    assert analysis.controls[2].default_value == 100
    assert captured["model"] == "gemini-3.1-flash-image"
    assert captured["method"] == "generateContent"
    response_format = captured["payload"]["generationConfig"]["responseFormat"]["text"]
    assert response_format["mimeType"] == "APPLICATION_JSON"
    assert "schema" in response_format
    assert len(inline_images) == 2


def test_remix_tweak_analysis_retries_conservatively_after_block(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
    )
    calls = []

    def fake_post(settings, model, method, payload):
        calls.append(payload)
        if len(calls) == 1:
            return gemini_blocked_response()
        return gemini_tweak_analysis_response()

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)

    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        analysis = analyze_remix_tweaks(session, live_settings, media_ids[0], media_ids[1])

    original_parts = calls[0]["contents"][0]["parts"]
    conservative_parts = calls[1]["contents"][0]["parts"]
    assert analysis.source == "conservative"
    assert analysis.notice == (
        "Gemini blocked detailed analysis for this pair, "
        "so conservative safe tweak suggestions are shown instead."
    )
    assert [control.id for control in analysis.controls] == [
        "caption-energy",
        "source-balance",
        "chaos-level",
        "victory-smirk",
    ]
    assert any("inline_data" in part for part in original_parts)
    assert not any("inline_data" in part for part in conservative_parts)
    assert "Conservative source cues" in conservative_parts[0]["text"]
    assert '"id":' not in conservative_parts[0]["text"]
    assert '"side": "Left"' in conservative_parts[0]["text"]
    assert '"side": "Right"' in conservative_parts[0]["text"]
    assert "visible_text" not in conservative_parts[0]["text"]


def test_conservative_tweak_analysis_uses_panel_side_labels(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
    )
    calls = []
    left_media_id = 0
    right_media_id = 0

    def fake_post(settings, model, method, payload):
        calls.append(payload)
        if len(calls) == 1:
            return gemini_blocked_response()
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "summary": f"Image {left_media_id} and Image {right_media_id} defaults.",
                                        "controls": [
                                            {
                                                "id": "style-balance",
                                                "label": f"Image {left_media_id} Style",
                                                "description": (
                                                    f"Blend Image {left_media_id} style into Image {right_media_id} style."
                                                ),
                                                "low_label": f"Image {left_media_id} style",
                                                "high_label": f"Image {right_media_id} style",
                                                "default_value": 50,
                                            }
                                        ],
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)

    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        left_media_id = media_ids[0]
        right_media_id = media_ids[1]
        analysis = analyze_remix_tweaks(session, live_settings, left_media_id, right_media_id)

    control = analysis.controls[0]
    control_text = " ".join([analysis.summary, control.label, control.description, control.low_label, control.high_label])

    assert analysis.source == "conservative"
    assert analysis.summary == "Left and Right defaults."
    assert control.label == "Left Style"
    assert control.description == "Blend Left style into Right style."
    assert control.low_label == "Left style"
    assert control.high_label == "Right style"
    assert f"Image {left_media_id}" not in control_text
    assert f"Image {right_media_id}" not in control_text


def test_remix_tweak_analysis_returns_local_fallback_when_conservative_blocks(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
    )
    calls = []

    def fake_post(settings, model, method, payload):
        calls.append(payload)
        return gemini_blocked_response()

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)

    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        analysis = analyze_remix_tweaks(session, live_settings, media_ids[0], media_ids[1])

    assert len(calls) == 2
    assert analysis.source == "local_fallback"
    assert "Gemini blocked detailed analysis" in analysis.notice
    assert "safe tweak suggestions" in analysis.notice
    assert analysis.summary == "Using safe general remix controls for this pair."
    assert [control.id for control in analysis.controls] == [
        "source-balance",
        "text-weight",
        "energy",
        "twist-level",
    ]


def test_remix_tweak_analysis_returns_local_fallback_when_gemini_api_fails(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
    )
    calls = []

    def fake_post(settings, model, method, payload):
        calls.append(payload)
        raise GeminiApiError("spend cap reached", status_code=429, status="RESOURCE_EXHAUSTED")

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)

    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        analysis = analyze_remix_tweaks(session, live_settings, media_ids[0], media_ids[1])

    assert len(calls) == 1
    assert analysis.source == "local_fallback"
    assert analysis.notice == "Gemini is cooling down, so safe default tweaks are shown for now."
    assert analysis.summary == "Using safe general remix controls for this pair."
    assert [control.id for control in analysis.controls] == [
        "source-balance",
        "text-weight",
        "energy",
        "twist-level",
    ]


def test_remix_prompt_includes_selected_tweak_values(settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    tweaks = remix_tweaks_from_payload(
        {
            "analysis_prompt_version": REMIX_TWEAK_ANALYSIS_PROMPT_VERSION,
            "summary": "Lean into the safe caption-driven version.",
            "controls": [
                {
                    "id": "caption-energy",
                    "label": "Caption energy",
                    "description": "How much text carries the joke.",
                    "low_label": "visual gag",
                    "high_label": "text-forward",
                    "default_value": 62,
                },
                {
                    "id": "unsafe",
                    "label": "Terrorism intensity",
                    "description": "Should be removed.",
                    "low_label": "less",
                    "high_label": "more",
                    "default_value": 50,
                },
            ],
            "values": {"caption-energy": 72},
            "no_text": True,
            "comic_strip": True,
            "anti_slop": True,
        }
    )

    with session_scope(settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        left = _source_metadata(session, settings, session.get(MediaItem, media_ids[0]))
        right = _source_metadata(session, settings, session.get(MediaItem, media_ids[1]))

    prompt = _remix_prompt(left, right, tweaks)

    assert "User-selected remix tweak sliders" in prompt
    assert "Sliders complement" in prompt
    assert "caption-energy" in prompt
    assert '"value": 72' in prompt
    assert "Terrorism intensity" not in prompt
    assert '"no_text"' not in prompt
    assert '"comic_strip"' not in prompt
    assert '"anti_slop"' not in prompt


def test_remix_prompt_includes_schizo_level(settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    tweaks = remix_tweaks_from_payload({"schizo_level": 3, "controls": [], "values": {}})
    assert tweaks is not None

    with session_scope(settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        left = _source_metadata(session, settings, session.get(MediaItem, media_ids[0]))
        right = _source_metadata(session, settings, session.get(MediaItem, media_ids[1]))

    prompt = _remix_prompt(left, right, tweaks)

    assert tweaks.schizo_level == 3
    assert "Surreal association level 3" in prompt
    assert "highly associative maximal meme collage" in prompt
    assert "Schizo mode" not in prompt
    assert '"schizo_level": 3' not in prompt


def test_remix_prompt_includes_chronically_online_level(settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    tweaks = remix_tweaks_from_payload({"chronically_online_level": 3, "controls": [], "values": {}})
    assert tweaks is not None

    with session_scope(settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        left = _source_metadata(session, settings, session.get(MediaItem, media_ids[0]))
        right = _source_metadata(session, settings, session.get(MediaItem, media_ids[1]))

    prompt = _remix_prompt(left, right, tweaks)

    assert tweaks.chronically_online_level == 3
    assert "Internet-culture density level 3" in prompt
    assert "deeply internet-native" in prompt
    assert "Chronically Online mode" not in prompt
    assert '"chronically_online_level": 3' not in prompt


def test_remix_prompt_proofreads_visible_text_when_text_allowed(settings, fixture_dir):
    prepare_archive(settings, fixture_dir)

    with session_scope(settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        left = _source_metadata(session, settings, session.get(MediaItem, media_ids[0]))
        right = _source_metadata(session, settings, session.get(MediaItem, media_ids[1]))

    prompt = _remix_prompt(left, right)

    assert "Visible text quality when text is allowed" in prompt
    assert "draft the exact wording before rendering it" in prompt
    assert "internal copy-edit and proofreading pass" in prompt
    assert "check spelling, grammar, punctuation" in prompt
    assert "sentence clarity" in prompt
    assert "Set JSON visible_text to the exact final text rendered in the image" in prompt


def test_remix_prompt_includes_hard_composition_flags(settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    tweaks = remix_tweaks_from_payload(
        {"no_text": True, "comic_strip": True, "anti_slop": True, "controls": [], "values": {}}
    )
    assert tweaks is not None

    with session_scope(settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        left = _source_metadata(session, settings, session.get(MediaItem, media_ids[0]))
        right = _source_metadata(session, settings, session.get(MediaItem, media_ids[1]))

    prompt = _remix_prompt(left, right, tweaks)

    assert tweaks.no_text is True
    assert tweaks.comic_strip is True
    assert tweaks.anti_slop is True
    assert "High-priority user composition constraints" in prompt
    assert "generated image must contain no visible words" in prompt
    assert "Set the returned JSON visible_text value to an empty string" in prompt
    assert "compose the generated image as a comic strip" in prompt
    assert "coherent narrative from start to finish" in prompt
    assert "setup, escalation or turn, and payoff" in prompt
    assert "silent comic strip with no visible words" in prompt
    assert "make the generated meme feel deliberately composed rather than generic AI output" in prompt
    assert "one clear focal point" in prompt
    assert "meme-native framing" in prompt
    assert "warped filler objects" in prompt
    assert "not portfolio-art generic" in prompt
    assert "Visible text quality when text is allowed" not in prompt


def test_remix_prompt_avoids_safety_triggering_creativity_language(settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    tweaks = remix_tweaks_from_payload(
        {"schizo_level": 2, "chronically_online_level": 2, "controls": [], "values": {}}
    )
    assert tweaks is not None

    with session_scope(settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        left = _source_metadata(session, settings, session.get(MediaItem, media_ids[0]))
        right = _source_metadata(session, settings, session.get(MediaItem, media_ids[1]))

    prompt = _remix_prompt(left, right, tweaks)

    assert "shy away from offense" not in prompt
    assert "all good art is somewhat offensive" not in prompt
    assert "internet-poisoned" not in prompt
    assert "non-hateful" in prompt


def test_remix_prompt_filters_unsafe_source_metadata(settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    unsafe_summary = "A dark 9/11 terrorism joke about the second tower"
    unsafe_visible_text = "they just hit the second tower"

    with session_scope(settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        classification = session.scalar(select(Classification).where(Classification.media_item_id == media_ids[0]))
        classification.summary = unsafe_summary
        classification.visible_text = unsafe_visible_text
        classification.topics_json = ["9/11", "candy"]
        classification.keywords_json = ["terrorism", "Cell Sours"]
        classification.search_phrases_json = ["second tower meme", "phone candy"]
        left = _source_metadata(session, settings, session.get(MediaItem, media_ids[0]))
        right = _source_metadata(session, settings, session.get(MediaItem, media_ids[1]))

    prompt = _remix_prompt(left, right)

    assert unsafe_summary not in prompt
    assert unsafe_visible_text not in prompt
    assert "terrorism" not in prompt
    assert "Cell Sours" in prompt
    assert "phone candy" in prompt
    assert "Harmless absurdity is key" in prompt
    assert "ephemeral nature of memes" in prompt


def test_remix_fallback_metadata_uses_safe_source_terms(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
    )
    image_only_response = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": base64.b64encode(png_data_url_bytes()).decode("ascii"),
                            }
                        }
                    ]
                }
            }
        ]
    }
    monkeypatch.setattr("meme_arsenal.remix.post_google_model", lambda *_args, **_kwargs: image_only_response)

    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        classification = session.scalar(select(Classification).where(Classification.media_item_id == media_ids[0]))
        classification.summary = "A dark 9/11 terrorism joke about the second tower"
        classification.visible_text = "they just hit the second tower"
        classification.topics_json = ["9/11", "candy"]
        classification.keywords_json = ["terrorism", "Cell Sours"]
        classification.search_phrases_json = ["second tower meme", "phone candy"]
        result = generate_remix(session, live_settings, media_ids[0], media_ids[1], job_id="safe-fallback")

    metadata_text = json.dumps(result.metadata.model_dump())
    assert result.generation_provenance.metadata_origin == "local_fallback"
    assert "9/11" not in metadata_text
    assert "terrorism" not in metadata_text
    assert "second tower" not in metadata_text
    assert "Cell Sours" in result.metadata.summary
    assert "phone candy" in result.metadata.search_phrases


def test_remix_response_with_null_parts_reports_no_image(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
    )
    monkeypatch.setattr(
        "meme_arsenal.remix.post_google_model",
        lambda *_args, **_kwargs: {"candidates": [{"content": {"parts": None}}]},
    )

    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        with pytest.raises(RemixNoImageError, match="did not include an image") as exc_info:
            generate_remix(session, live_settings, media_ids[0], media_ids[1], job_id="null-parts")
    assert exc_info.value.rejection_context["failure_type"] == "no_image_returned"


def test_remix_mitigation_plan_targets_riskier_side(settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    unsafe_summary = "A dark 9/11 terrorism joke about the second tower"

    with session_scope(settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        classification = session.scalar(select(Classification).where(Classification.media_item_id == media_ids[0]))
        classification.summary = unsafe_summary
        classification.keywords_json = ["terrorism", "Cell Sours"]
        left = _source_metadata(session, settings, session.get(MediaItem, media_ids[0]))
        right = _source_metadata(session, settings, session.get(MediaItem, media_ids[1]))
        plan = build_remix_mitigation_plan(session, settings, media_ids[0], media_ids[1])

    assert [mitigation.id if mitigation else "original" for mitigation in plan] == [
        "original",
        "soften-left",
        "soften-both",
        "visual-only",
        "metadata-only",
    ]
    prompt = _remix_prompt(left, right, mitigation=plan[1])
    assert "Retry mitigation" in prompt
    assert '"soften_left": true' in prompt
    assert unsafe_summary not in prompt
    assert "terrorism" not in prompt
    assert "Cell Sours" in prompt


def test_safer_remix_mitigation_plan_starts_softened(settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    unsafe_summary = "A dark 9/11 terrorism joke about the second tower"

    with session_scope(settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        classification = session.scalar(select(Classification).where(Classification.media_item_id == media_ids[0]))
        classification.summary = unsafe_summary
        classification.visible_text = "they just hit the second tower"
        classification.keywords_json = ["terrorism", "Cell Sours"]
        left = _source_metadata(session, settings, session.get(MediaItem, media_ids[0]))
        right = _source_metadata(session, settings, session.get(MediaItem, media_ids[1]))
        plan = build_remix_mitigation_plan(
            session,
            settings,
            media_ids[0],
            media_ids[1],
            safety_mode=REMIX_SAFETY_MODE_SAFER,
        )

    assert [mitigation.id if mitigation else "original" for mitigation in plan] == [
        "safer-start",
        "visual-only",
        "metadata-only",
    ]
    prompt = _remix_prompt(left, right, mitigation=plan[0])
    assert "Retry mitigation" in prompt
    assert '"soften_left": true' in prompt
    assert '"soften_right": true' in prompt
    assert unsafe_summary not in prompt
    assert "second tower" not in prompt
    assert "terrorism" not in prompt
    assert "Cell Sours" in prompt


def test_local_remix_job_does_not_mitigate_invalid_credentials(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    settings = replace(settings, ai_provider="google", google_api_key="test-only")
    calls = []

    def rejected(*_args, **_kwargs):
        calls.append(True)
        raise GeminiApiError("API key not valid", status_code=400)

    monkeypatch.setattr("meme_arsenal.remix_server.generate_remix", rejected)
    with session_scope(settings) as db:
        ids = list(db.scalars(select(MediaItem.id)))
    store = RemixJobStore(settings)
    job = store.create_job(*ids)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and store.get(job.id).status != "failed":
        time.sleep(0.05)
    current = store.get(job.id)
    assert current.status == "failed"
    assert len(current.attempts) == 1
    assert calls == [True]
    assert "API key" in current.suggested_retry


def test_remix_job_retries_with_mitigation_after_block(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
    )
    responses = [gemini_blocked_response(), gemini_image_response("Mitigated remix")]

    def fake_post(settings, model, method, payload):
        return responses.pop(0)

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)

    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()

    store = RemixJobStore(live_settings)
    job = store.create_job(media_ids[0], media_ids[1])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = store.get(job.id)
        if current and current.status in {"ready", "blocked", "failed", "rate_limited"}:
            break
        time.sleep(0.05)

    current = store.get(job.id)
    assert current is not None
    assert current.status == "ready"
    assert current.result is not None
    assert current.result.metadata.summary == "Mitigated remix"
    assert [attempt.status for attempt in current.attempts] == ["failed", "succeeded"]
    assert current.attempts[0].mitigation_id == "original"
    assert current.attempts[1].mitigation_id.startswith("soften-")
    assert current.failure_explanation is None
    assert current.suggested_retry is None


def test_remix_job_explains_blocked_attempt_in_background(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
    )
    responses = [gemini_blocked_response(), gemini_image_response("Mitigated remix")]
    explained = threading.Event()
    contexts = []

    def fake_post(settings, model, method, payload):
        return responses.pop(0)

    def fake_explain(settings, context):
        contexts.append(context)
        explained.set()
        return "Political mockery"

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)
    monkeypatch.setattr("meme_arsenal.remix_server.explain_remix_rejection", fake_explain)

    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()

    store = RemixJobStore(live_settings)
    try:
        job = store.create_job(media_ids[0], media_ids[1])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = store.get(job.id)
            if current and current.status in {"ready", "blocked", "failed", "rate_limited"}:
                break
            time.sleep(0.05)

        assert explained.wait(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = store.get(job.id)
            if current and current.attempts[0].rejection_reason == "Political mockery":
                break
            time.sleep(0.05)

        current = store.get(job.id)
        assert current is not None
        assert current.status == "ready"
        assert current.result is not None
        assert [attempt.status for attempt in current.attempts] == ["failed", "succeeded"]
        assert current.attempts[0].block_reason == "IMAGE_SAFETY"
        assert current.attempts[0].rejection_reason == "Political mockery"
        assert current.rejection_reason == "Political mockery"
        assert contexts[0]["block_reason"] == "IMAGE_SAFETY"
        assert len(responses) == 0
    finally:
        store.shutdown()


def test_remix_job_explains_no_image_attempt_in_background(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
    )
    responses = [
        {"candidates": [{"content": {"parts": [{"text": "I can summarize the idea, but no image is attached."}]}}]},
        gemini_image_response("No-image retry remix"),
    ]
    explained = threading.Event()
    contexts = []

    def fake_post(settings, model, method, payload):
        return responses.pop(0)

    def fake_explain(settings, context):
        contexts.append(context)
        explained.set()
        return "Text-only response"

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)
    monkeypatch.setattr("meme_arsenal.remix_server.explain_remix_rejection", fake_explain)

    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()

    store = RemixJobStore(live_settings)
    try:
        job = store.create_job(media_ids[0], media_ids[1])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = store.get(job.id)
            if current and current.status in {"ready", "blocked", "failed", "rate_limited"}:
                break
            time.sleep(0.05)

        assert explained.wait(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = store.get(job.id)
            if current and current.attempts[0].rejection_reason == "Text-only response":
                break
            time.sleep(0.05)

        current = store.get(job.id)
        assert current is not None
        assert current.status == "ready"
        assert current.result is not None
        assert current.result.metadata.summary == "No-image retry remix"
        assert [attempt.status for attempt in current.attempts] == ["failed", "succeeded"]
        assert current.attempts[0].message == "No image returned"
        assert current.attempts[0].rejection_reason == "Text-only response"
        assert current.rejection_reason == "Text-only response"
        assert contexts[0]["failure_type"] == "no_image_returned"
        assert contexts[0]["response"]["text_excerpt"].startswith("I can summarize")
        assert len(responses) == 0
    finally:
        store.shutdown()


def test_remix_job_uses_metadata_only_fallback_after_visual_blocks(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
    )
    calls = []
    responses = [
        gemini_blocked_response(),
        gemini_blocked_response(),
        gemini_blocked_response(),
        gemini_blocked_response(),
        gemini_image_response("Metadata-only remix"),
    ]

    def fake_post(settings, model, method, payload):
        calls.append(payload)
        return responses.pop(0)

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)

    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()

    store = RemixJobStore(live_settings)
    job = store.create_job(media_ids[0], media_ids[1])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = store.get(job.id)
        if current and current.status in {"ready", "blocked", "failed", "rate_limited"}:
            break
        time.sleep(0.05)

    current = store.get(job.id)
    assert current is not None
    assert current.status == "ready"
    assert current.result is not None
    assert current.result.metadata.summary == "Metadata-only remix"
    assert [attempt.mitigation_id for attempt in current.attempts] == [
        "original",
        current.attempts[1].mitigation_id,
        "soften-both",
        "visual-only",
        "metadata-only",
    ]
    assert current.attempts[1].mitigation_id.startswith("soften-")
    assert [attempt.status for attempt in current.attempts] == [
        "failed",
        "failed",
        "failed",
        "failed",
        "succeeded",
    ]

    metadata_parts = calls[-1]["contents"][0]["parts"]
    metadata_prompt = metadata_parts[0]["text"]
    assert "metadata-only" in metadata_prompt
    assert "No source visual is attached" in metadata_prompt
    assert not any("inline_data" in part for part in metadata_parts)


def test_video_remix_payload_includes_preview_keyframes_and_original_video(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
        max_video_keyframes=2,
    )
    captured = {}

    def write_image(path, color):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (96, 54), color).save(path, "JPEG")

    def fake_post(settings, model, method, payload):
        captured["payload"] = payload
        return gemini_image_response()

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)

    with session_scope(live_settings) as session:
        message = TelegramMessage(
            telegram_message_id=900,
            chat_id="fixture",
            date=datetime(2026, 1, 6, tzinfo=timezone.utc),
            caption="video remix source",
        )
        session.add(message)
        session.flush()
        original = live_settings.originals_dir / "video.mp4"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"sent-to-gemini-as-video-context")
        preview = live_settings.previews_dir / "video-preview.jpg"
        first_frame = live_settings.keyframes_dir / "video" / "frame-000.jpg"
        second_frame = live_settings.keyframes_dir / "video" / "frame-001.jpg"
        write_image(preview, (20, 90, 140))
        write_image(first_frame, (80, 20, 140))
        write_image(second_frame, (140, 90, 20))
        video = MediaItem(
            telegram_message_id=message.id,
            media_type="video",
            mime_type="video/mp4",
            original_path=reserve_relative(live_settings, original),
            preview_path=reserve_relative(live_settings, preview),
            processing_status="processed",
        )
        session.add(video)
        session.flush()
        session.add_all(
            [
                MediaKeyframe(
                    media_item_id=video.id,
                    frame_index=0,
                    timestamp_seconds=0,
                    path=reserve_relative(live_settings, first_frame),
                ),
                MediaKeyframe(
                    media_item_id=video.id,
                    frame_index=1,
                    timestamp_seconds=2,
                    path=reserve_relative(live_settings, second_frame),
                ),
            ]
        )
        right_id = session.scalars(select(MediaItem.id).where(MediaItem.id != video.id).order_by(MediaItem.id)).first()
        result = generate_remix(session, live_settings, video.id, right_id, job_id="video-rich-inputs")

    parts = captured["payload"]["contents"][0]["parts"]
    inline_media = [part["inline_data"] for part in parts if "inline_data" in part]
    inline_images = [part for part in inline_media if part["mime_type"].startswith("image/")]
    inline_videos = [part for part in inline_media if part["mime_type"].startswith("video/")]
    text_parts = [part["text"] for part in parts if "text" in part]

    assert len(inline_images) == 4
    assert len(inline_videos) == 1
    assert result.generation_provenance.attachment_mode == "original-video"
    assert inline_videos[0]["mime_type"] == "video/mp4"
    assert inline_videos[0]["data"] == base64.b64encode(b"sent-to-gemini-as-video-context").decode("ascii")
    assert any("Left source visuals follow. Media type: video. Visual count: 3." in text for text in text_parts)
    assert any("Left source visual 3." in text for text in text_parts)
    assert all(image["mime_type"] == "image/jpeg" for image in inline_images[:3])


def test_video_remix_retry_drops_original_video_after_block(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
        max_video_keyframes=2,
    )
    calls = []
    responses = [gemini_blocked_response(), gemini_image_response("Stills fallback remix")]

    def write_image(path, color):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (96, 54), color).save(path, "JPEG")

    def fake_post(settings, model, method, payload):
        calls.append(payload)
        return responses.pop(0)

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)

    with session_scope(live_settings) as session:
        message = TelegramMessage(
            telegram_message_id=902,
            chat_id="fixture",
            date=datetime(2026, 1, 8, tzinfo=timezone.utc),
            caption="video remix source",
        )
        session.add(message)
        session.flush()
        original = live_settings.originals_dir / "blocked-video.mp4"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"video-that-blocks-first-attempt")
        preview = live_settings.previews_dir / "blocked-video-preview.jpg"
        first_frame = live_settings.keyframes_dir / "blocked-video" / "frame-000.jpg"
        second_frame = live_settings.keyframes_dir / "blocked-video" / "frame-001.jpg"
        write_image(preview, (20, 90, 140))
        write_image(first_frame, (80, 20, 140))
        write_image(second_frame, (140, 90, 20))
        video = MediaItem(
            telegram_message_id=message.id,
            media_type="video",
            mime_type="video/mp4",
            original_path=reserve_relative(live_settings, original),
            preview_path=reserve_relative(live_settings, preview),
            processing_status="processed",
        )
        session.add(video)
        session.flush()
        session.add_all(
            [
                MediaKeyframe(
                    media_item_id=video.id,
                    frame_index=0,
                    timestamp_seconds=3.0,
                    path=reserve_relative(live_settings, first_frame),
                ),
                MediaKeyframe(
                    media_item_id=video.id,
                    frame_index=1,
                    timestamp_seconds=6.0,
                    path=reserve_relative(live_settings, second_frame),
                ),
            ]
        )
        right_id = session.scalars(select(MediaItem.id).where(MediaItem.id != video.id).order_by(MediaItem.id)).first()
        media_ids = (video.id, right_id)

    store = RemixJobStore(live_settings)
    job = store.create_job(media_ids[0], media_ids[1])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = store.get(job.id)
        if current and current.status in {"ready", "blocked", "failed", "rate_limited"}:
            break
        time.sleep(0.05)

    current = store.get(job.id)
    assert current is not None
    assert current.status == "ready"
    assert current.result is not None
    assert current.result.metadata.summary == "Stills fallback remix"
    assert len(calls) == 2
    first_inline = [part["inline_data"] for part in calls[0]["contents"][0]["parts"] if "inline_data" in part]
    second_inline = [part["inline_data"] for part in calls[1]["contents"][0]["parts"] if "inline_data" in part]
    assert any(part["mime_type"] == "video/mp4" for part in first_inline)
    assert not any(part["mime_type"] == "video/mp4" for part in second_inline)
    assert sum(part["mime_type"].startswith("image/") for part in second_inline) == 4
    assert [attempt.status for attempt in current.attempts] == ["failed", "succeeded"]


def test_safer_video_remix_starts_with_stills_only_and_visual_fallback(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
        max_video_keyframes=2,
    )
    calls = []
    responses = [gemini_blocked_response(), gemini_image_response("Safer stills remix")]

    def write_image(path, color):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (96, 54), color).save(path, "JPEG")

    def fake_post(settings, model, method, payload):
        calls.append(payload)
        return responses.pop(0)

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)

    with session_scope(live_settings) as session:
        message = TelegramMessage(
            telegram_message_id=903,
            chat_id="fixture",
            date=datetime(2026, 1, 9, tzinfo=timezone.utc),
            caption="safety sensitive video remix source",
        )
        session.add(message)
        session.flush()
        original = live_settings.originals_dir / "safer-video.mp4"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"this-video-should-not-be-sent-in-safer-mode")
        preview = live_settings.previews_dir / "safer-video-preview.jpg"
        first_frame = live_settings.keyframes_dir / "safer-video" / "frame-000.jpg"
        second_frame = live_settings.keyframes_dir / "safer-video" / "frame-001.jpg"
        write_image(preview, (20, 90, 140))
        write_image(first_frame, (80, 20, 140))
        write_image(second_frame, (140, 90, 20))
        video = MediaItem(
            telegram_message_id=message.id,
            media_type="video",
            mime_type="video/mp4",
            original_path=reserve_relative(live_settings, original),
            preview_path=reserve_relative(live_settings, preview),
            processing_status="processed",
        )
        session.add(video)
        session.flush()
        session.add_all(
            [
                MediaKeyframe(
                    media_item_id=video.id,
                    frame_index=0,
                    timestamp_seconds=1.0,
                    path=reserve_relative(live_settings, first_frame),
                ),
                MediaKeyframe(
                    media_item_id=video.id,
                    frame_index=1,
                    timestamp_seconds=4.0,
                    path=reserve_relative(live_settings, second_frame),
                ),
                Classification(
                    media_item_id=video.id,
                    model=live_settings.ai_model,
                    prompt_version=REMIX_PROMPT_VERSION,
                    summary="A dark 9/11 terrorism joke about the second tower",
                    visible_text="they just hit the second tower",
                    meme_format="reaction meme",
                    keywords_json=["terrorism", "Cell Sours"],
                ),
            ]
        )
        right_id = session.scalars(select(MediaItem.id).where(MediaItem.id != video.id).order_by(MediaItem.id)).first()
        media_ids = (video.id, right_id)

    store = RemixJobStore(live_settings)
    job = store.create_job(media_ids[0], media_ids[1], safety_mode=REMIX_SAFETY_MODE_SAFER)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = store.get(job.id)
        if current and current.status in {"ready", "blocked", "failed", "rate_limited"}:
            break
        time.sleep(0.05)

    current = store.get(job.id)
    assert current is not None
    assert current.status == "ready"
    assert current.safety_mode == REMIX_SAFETY_MODE_SAFER
    assert current.snapshot()["safety_mode"] == REMIX_SAFETY_MODE_SAFER
    assert current.result is not None
    assert current.result.safety_mode == REMIX_SAFETY_MODE_SAFER
    assert current.result.payload()["safety_mode"] == REMIX_SAFETY_MODE_SAFER
    assert current.result.metadata.summary == "Safer stills remix"
    assert [attempt.mitigation_id for attempt in current.attempts] == ["safer-start", "visual-only"]
    assert [attempt.status for attempt in current.attempts] == ["failed", "succeeded"]
    assert len(calls) == 2

    first_parts = calls[0]["contents"][0]["parts"]
    second_parts = calls[1]["contents"][0]["parts"]
    first_prompt = first_parts[0]["text"]
    second_prompt = second_parts[0]["text"]
    first_inline = [part["inline_data"] for part in first_parts if "inline_data" in part]
    second_inline = [part["inline_data"] for part in second_parts if "inline_data" in part]

    assert "safer-start" in first_prompt
    assert "visual-only" in second_prompt
    assert "terrorism" not in first_prompt
    assert "second tower" not in first_prompt
    assert not any(part["mime_type"] == "video/mp4" for part in first_inline)
    assert not any(part["mime_type"] == "video/mp4" for part in second_inline)
    assert sum(part["mime_type"].startswith("image/") for part in first_inline) == 4
    assert sum(part["mime_type"].startswith("image/") for part in second_inline) == 4


def test_document_remix_uses_readable_preview_not_original(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
    )
    captured = {}

    def fake_post(settings, model, method, payload):
        captured["payload"] = payload
        return gemini_image_response()

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)

    with session_scope(live_settings) as session:
        message = TelegramMessage(
            telegram_message_id=901,
            chat_id="fixture",
            date=datetime(2026, 1, 7, tzinfo=timezone.utc),
            caption="document with image preview",
        )
        session.add(message)
        session.flush()
        original = live_settings.originals_dir / "notes.pdf"
        preview = live_settings.previews_dir / "notes-preview.jpg"
        original.parent.mkdir(parents=True, exist_ok=True)
        preview.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"%PDF-pretend")
        Image.new("RGB", (80, 80), (50, 120, 160)).save(preview, "JPEG")
        document = MediaItem(
            telegram_message_id=message.id,
            media_type="document",
            mime_type="application/pdf",
            original_path=reserve_relative(live_settings, original),
            preview_path=reserve_relative(live_settings, preview),
            processing_status="processed",
        )
        session.add(document)
        session.flush()
        right_id = session.scalars(select(MediaItem.id).where(MediaItem.id != document.id).order_by(MediaItem.id)).first()
        generate_remix(session, live_settings, document.id, right_id, job_id="document-preview-input")

    parts = captured["payload"]["contents"][0]["parts"]
    inline_images = [part["inline_data"] for part in parts if "inline_data" in part]
    text_parts = [part["text"] for part in parts if "text" in part]

    assert len(inline_images) == 2
    assert inline_images[0]["mime_type"] == "image/jpeg"
    assert inline_images[0]["data"] != base64.b64encode(b"%PDF-pretend").decode("ascii")
    assert any("Left source visuals follow. Media type: document. Visual count: 1." in text for text in text_parts)


def test_save_remix_persists_catalog_item_and_embedding(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
        embedding_model="gemini-embedding-2",
    )

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", lambda *_args, **_kwargs: gemini_image_response())
    def fail_classification(*_args, **_kwargs):
        raise RuntimeError("explicit independent classification failure")
    monkeypatch.setattr("meme_arsenal.remix.classify_media_item", fail_classification)
    monkeypatch.setattr(
        "meme_arsenal.embeddings.post_google_model",
        lambda *_args, **_kwargs: {"embedding": {"values": [0.1] * live_settings.embedding_dimensions}},
    )

    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        result = generate_remix(session, live_settings, media_ids[0], media_ids[1], job_id="save-test")
        saved = save_remix_result(session, live_settings, result)
        source_payload = catalog_item_payload(session, live_settings, media_ids[0])
        remix_message = session.scalar(select(TelegramMessage).where(TelegramMessage.chat_id == REMIX_CHAT_ID))
        remix_media = session.get(MediaItem, saved["item"]["id"])
        remix_classification = session.scalar(
            select(Classification).where(Classification.media_item_id == saved["item"]["id"])
        )

        assert session.scalar(select(func.count()).select_from(Embedding)) == 3
        output_embedding = session.scalar(select(Embedding).where(Embedding.media_item_id == remix_media.id))
        assert output_embedding.execution_provider == "google"
        assert output_embedding.embedding_model == live_settings.embedding_model
        assert output_embedding.source_text_hash == embedding_text_hash(
            embedding_source_text(session, remix_media, remix_classification))
        session.expire_all()
        assert is_saved_generation_authoritative(remix_media, remix_classification)
        assert embed_media_item(session, live_settings, remix_media).id == output_embedding.id

    assert saved["embedding_error"] is None
    assert saved["item"]["summary"] == "Deploy smug remix"
    assert not saved["item"]["summary"].lower().startswith("remix of ")
    assert saved["item"]["media_type"] == "image"
    assert saved["item"]["original_path"].endswith("media/originals/remixes/save-test.png")
    assert [source["id"] for source in saved["item"]["remix_sources"]] == media_ids
    assert all(source["title"] for source in saved["item"]["remix_sources"])
    assert all(source["thumbnail_path"] or source["preview_path"] or source["original_path"] for source in saved["item"]["remix_sources"])
    assert saved["item"]["remix_used_by"] == []
    assert [used_by["id"] for used_by in source_payload["remix_used_by"]] == [saved["item"]["id"]]
    assert source_payload["remix_used_by"][0]["title"] == "Deploy smug remix"
    assert remix_message is not None
    assert remix_message.raw_metadata_json["source_media_item_ids"] == media_ids
    assert remix_media is not None
    assert remix_media.processing_status == "processed"
    assert remix_classification is not None
    assert (remix_classification.model, remix_classification.prompt_version) == SLOT
    assert remix_classification.execution_provider is None
    assert remix_classification.compatibility_key is None


@pytest.mark.parametrize("origin", ["model_json", "local_fallback", "absent"])
@pytest.mark.parametrize("independent", [True, False])
@pytest.mark.parametrize("embedding_failure", [False, True])
def test_save_remix_keeps_actual_generation_and_current_operations(
    monkeypatch, settings, fixture_dir, origin, independent, embedding_failure,
):
    from meme_arsenal.ai import classify_media_item
    from meme_arsenal.embeddings import select_embedding_input

    prepare_archive(settings, fixture_dir)
    config_a = replace(settings, remix_provider="google", google_api_key="test-key",
                       remix_model="models/" + "a" * 127, remix_style_guidance="historical generation A")
    config_b = replace(settings, ai_model="current-classifier", classification_provider="google",
                       google_api_key="current-key", classification_prompt_version="current-prompt",
                       embedding_provider="google", embedding_model="current-embedding",
                       classification_style_guidance="current classification B", remix_style_guidance="current remix B")
    generation_calls = []
    def generate(config, model, *args, **kwargs):
        generation_calls.append((config.effective_remix_provider, model))
        return gemini_image_response()
    monkeypatch.setattr("meme_arsenal.remix.post_google_model", generate)
    c_before = {}
    def adapter(config, context):
        assert config.ai_model == config_b.ai_model
        assert config.classification_prompt_version == config_b.classification_prompt_version
        assert config.classification_style_guidance == "current classification B"
        payload = ClassificationPayload.model_validate(json.loads(
            gemini_image_response()["candidates"][0]["content"]["parts"][0]["text"]))
        return payload.model_copy(update={"summary": "   "}), {"independent": "response"}
    monkeypatch.setattr("meme_arsenal.ai.classify_with_google", adapter)
    def classify(db, config, item, **kwargs):
        if not independent:
            return None
        row = classify_media_item(db, config, item, **kwargs)
        c_before.update({column.name: getattr(row, column.name) for column in Classification.__table__.columns})
        return row
    monkeypatch.setattr("meme_arsenal.remix.classify_media_item", classify)
    e_calls = []
    def embedding(config, model, *args, **kwargs):
        e_calls.append((config.effective_embedding_provider, model))
        if embedding_failure:
            raise RuntimeError("explicit embedding quota failure")
        return {"embedding": {"values": [0.2] * config.embedding_dimensions}}
    monkeypatch.setattr("meme_arsenal.embeddings.post_google_model", embedding)
    with session_scope(settings) as db:
        ids = list(db.scalars(select(MediaItem.id).order_by(MediaItem.id)))
        result = generate_remix(db, config_a, *ids, job_id=f"changed-{origin}-{independent}")
        provenance = result.generation_provenance
        provenance = replace(provenance, capture=replace(
            provenance.capture, policy_digest="a" * 64, algorithm_version="historical-policy"),
            metadata_origin="local_fallback" if origin == "local_fallback" else "model_json")
        result = replace(result, generation_provenance=None if origin == "absent" else provenance)
        saved = save_remix_result(db, config_b, result, export_static_catalog=False)
        from meme_arsenal.result_identity import generation_identity
        assert saved["item"]["execution_identity"]["generation"] == generation_identity(
            result.generation_provenance.payload() if result.generation_provenance else None)
        assert bool(saved["item"]["execution_identity"]["classification"]) == independent
        item = db.get(MediaItem, saved["item"]["id"])
        row = db.scalar(select(Classification).where(Classification.media_item_id == item.id))
        db.expire_all()
        assert generation_calls == [("google", config_a.remix_model)]
        expected_provenance = result.generation_provenance.payload() if result.generation_provenance else None
        assert row.raw_response_json["generation"]["provenance"] == expected_provenance
        message = db.get(TelegramMessage, item.telegram_message_id)
        assert message.raw_metadata_json["generation"] == {"provenance": expected_provenance}
        assert "current-key" not in json.dumps(message.raw_metadata_json)
        assert "database_url" not in json.dumps(message.raw_metadata_json)
        if independent:
            for name, value in c_before.items():
                if name not in {"raw_response_json", "created_at"}:
                    assert getattr(row, name) == value
            for name, value in c_before["raw_response_json"].items():
                assert row.raw_response_json[name] == value
            assert row.summary == "   "
            assert select_embedding_input(db, config_b, item).id == row.id
        else:
            assert (row.model, row.prompt_version) == SLOT
            assert row.summary == result.metadata.summary
            assert is_saved_generation_authoritative(item, row) == (origin == "model_json")
        assert bool(e_calls) == (independent or origin == "model_json")
        if e_calls:
            assert e_calls == [("google", config_b.embedding_model)]
        assert saved["embedding_error"] == (
            "explicit embedding quota failure" if e_calls and embedding_failure else None)
        assert item.processing_status == "processed"
        assert (config_b.reserve_path / item.original_path).exists()


def test_save_remix_can_defer_embedding_and_catalog_export(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
        embedding_model="gemini-embedding-2",
    )

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", lambda *_args, **_kwargs: gemini_image_response())

    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        result = generate_remix(session, live_settings, media_ids[0], media_ids[1], job_id="fast-save-test")

        def fail_if_called(*_args, **_kwargs):
            raise AssertionError("deferred save should not block on this work")

        monkeypatch.setattr("meme_arsenal.remix.embed_media_item", fail_if_called)
        monkeypatch.setattr("meme_arsenal.remix.export_catalog", fail_if_called)
        saved = save_remix_result(session, live_settings, result, embed=False, export_static_catalog=False)

    assert saved["embedding_error"] is None
    assert saved["item"]["summary"] == "Deploy smug remix"
    assert saved["item"]["original_path"].endswith("media/originals/remixes/fast-save-test.png")


def test_save_remix_retries_after_message_id_conflict(monkeypatch, settings, fixture_dir, tmp_path):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(settings, remix_model="gemini-3.1-flash-image")
    image_path = tmp_path / "conflict-draft.png"
    Image.new("RGB", (96, 96), (0, 80, 180)).save(image_path, "PNG")

    conflict_id = -100_000
    next_message_calls: list[int] = []

    def fake_next_id(_session: Any, *, seed: int | None = None) -> int:
        # Model a writer winning between lookup and insert. Known sequential
        # collisions are normally skipped by the real candidate lookup.
        next_message_calls.append(int(seed or 0))
        if len(next_message_calls) == 1:
            return conflict_id
        return conflict_id - 1

    with session_scope(live_settings) as session:
        original = TelegramMessage(
            telegram_message_id=conflict_id,
            chat_id=REMIX_CHAT_ID,
            date=datetime.now(timezone.utc),
            caption="reserved remix id",
        )
        session.add(original)
        session.flush()
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        original.caption = "pending caller edit"
        result = RemixResult(
            job_id="save-conflict-test",
            left_id=media_ids[0],
            right_id=media_ids[1],
            image_path=image_path,
            metadata=ClassificationPayload(
                summary="Deploy smug remix",
                visible_text="SHIP IT",
                narration="A generated remix under a db race condition.",
                language="en",
                meme_format="generated remix meme",
                tone=["playful"],
                reaction_type=["victory"],
                topics=["software", "deployment"],
                keywords=["deploy", "smug", "remix", "conflict"],
                people_or_characters=[],
                safety_flags=[],
                search_phrases=["deploy smug remix", "conflict replay"],
                confidence=0.93,
            ),
            raw_response={"ok": True},
        )

        monkeypatch.setattr("meme_arsenal.remix._next_remix_message_id", fake_next_id)
        saved = save_remix_result(
            session,
            live_settings,
            result,
            embed=False,
            export_static_catalog=False,
        )

        media_item = session.get(MediaItem, saved["item"]["id"])
        assert media_item is not None
        saved_message = session.get(TelegramMessage, media_item.telegram_message_id)
        assert saved_message is not None
        assert saved_message.telegram_message_id == conflict_id - 1
        assert session.get(TelegramMessage, original.id).caption == "pending caller edit"

    with session_scope(live_settings) as session:
        retained = session.scalar(select(TelegramMessage).where(
            TelegramMessage.chat_id == REMIX_CHAT_ID,
            TelegramMessage.telegram_message_id == conflict_id,
        ))
        assert retained is not None
        assert retained.caption == "pending caller edit"

    assert len(next_message_calls) == 2
    assert next_message_calls[0] != next_message_calls[1]
    assert saved["item"]["summary"] == "Deploy smug remix"
    assert saved["item"]["media_type"] == "image"


def test_save_remix_processing_failure_rolls_back_message(monkeypatch, settings, fixture_dir, tmp_path):
    prepare_archive(settings, fixture_dir)
    image = tmp_path / "failed-processing.png"
    Image.new("RGB", (96, 96)).save(image)
    with session_scope(settings) as db:
        ids = list(db.scalars(select(MediaItem.id).order_by(MediaItem.id)))
        before_messages = db.scalar(select(func.count()).select_from(TelegramMessage))
    result = RemixResult(
        job_id="failed-processing", left_id=ids[0], right_id=ids[1], image_path=image,
        metadata=ClassificationPayload.model_validate(
            json.loads(gemini_image_response()["candidates"][0]["content"]["parts"][0]["text"])),
        raw_response={},
    )

    def fail_processing(*_args, **_kwargs):
        raise RuntimeError("fixture processing failure")

    monkeypatch.setattr("meme_arsenal.remix.process_media_item", fail_processing)
    with pytest.raises(RuntimeError, match="fixture processing failure"):
        # This read-only caller has not started a physical SQLite transaction
        # before the helper creates its first SAVEPOINT.
        with session_scope(settings) as db:
            save_remix_result(db, settings, result, embed=False, export_static_catalog=False)
    with session_scope(settings) as db:
        assert db.scalar(select(func.count()).select_from(TelegramMessage)) == before_messages
        assert db.scalar(select(func.count()).select_from(MediaItem)) == len(ids)


def test_remix_job_store_real_save_collision_and_failure_rollback(
    monkeypatch, settings, fixture_dir, tmp_path,
):
    prepare_archive(settings, fixture_dir)
    image = tmp_path / "local.png"
    Image.new("RGB", (96, 96)).save(image)
    with session_scope(settings) as db:
        ids = list(db.scalars(select(MediaItem.id).order_by(MediaItem.id)))
        db.add(TelegramMessage(telegram_message_id=-777, chat_id=REMIX_CHAT_ID,
                              caption="reserved", date=datetime.now(timezone.utc)))
    result = RemixResult(
        job_id="local-real-save", left_id=ids[0], right_id=ids[1], image_path=image,
        metadata=ClassificationPayload.model_validate(
            json.loads(gemini_image_response()["candidates"][0]["content"]["parts"][0]["text"])),
        raw_response={},
    )
    from meme_arsenal import remix
    real_next = remix._next_remix_message_id
    calls = []
    def candidate(db, **kwargs):
        calls.append(1)
        return -777 if len(calls) == 1 else real_next(db, **kwargs)
    monkeypatch.setattr(remix, "_next_remix_message_id", candidate)
    store = RemixJobStore(settings)
    monkeypatch.setattr(store, "_finish_saved_item", lambda *_: None)
    try:
        store._jobs[result.job_id] = RemixJob(
            id=result.job_id, left_id=ids[0], right_id=ids[1], status="saving", result=result)
        store._save_job(result.job_id, result)
        assert store.get(result.job_id).status == "saved"
        saved_id = store.get(result.job_id).saved_item["id"]
        with session_scope(settings) as db:
            assert db.get(MediaItem, saved_id) is not None
        assert len(calls) == 2

        # Exhaustion is handled by the caller's transaction context and local
        # store restores ready. Filesystem copies are not transactional.
        monkeypatch.setattr(remix, "_next_remix_message_id", lambda *_a, **_k: -777)
        failed = replace(result, job_id="local-failed-save")
        store._jobs[failed.job_id] = RemixJob(
            id=failed.job_id, left_id=ids[0], right_id=ids[1], status="saving", result=failed)
        store._save_job(failed.job_id, failed)
        assert store.get(failed.job_id).status == "ready"
        assert "Please retry" in store.get(failed.job_id).error
        with session_scope(settings) as db:
            assert db.scalar(select(func.count()).select_from(MediaItem)) == len(ids) + 1
    finally:
        store.shutdown()


def test_remix_job_store_save_runs_in_background(monkeypatch, settings, tmp_path):
    image_path = tmp_path / "draft.png"
    Image.new("RGB", (96, 96), (90, 120, 160)).save(image_path, "PNG")
    result = RemixResult(
        job_id="background-save-test",
        left_id=1,
        right_id=2,
        image_path=image_path,
        metadata=ClassificationPayload(
            summary="Background saved remix",
            visible_text="SHIP IT",
            narration="A remix is being saved.",
            language="en",
            meme_format="generated remix meme",
            tone=["playful"],
            reaction_type=["victory"],
            topics=["software"],
            keywords=["deploy"],
            people_or_characters=[],
            safety_flags=[],
            search_phrases=["background save remix"],
            confidence=0.9,
        ),
        raw_response={"ok": True},
    )
    store = RemixJobStore(settings)
    store._jobs[result.job_id] = RemixJob(
        id=result.job_id,
        left_id=result.left_id,
        right_id=result.right_id,
        status="ready",
        message="Ready",
        result=result,
    )
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def fake_save_remix_result(session, settings_arg, result_arg, *, embed=True, export_static_catalog=True):
        assert result_arg is result
        assert embed is False
        assert export_static_catalog is False
        started.set()
        assert release.wait(timeout=5)
        return {"item": {"id": 123, "summary": "Background saved remix"}, "embedding_error": None}

    def fake_finish_saved_item(job_id, media_item_id):
        assert job_id == result.job_id
        assert media_item_id == 123
        finished.set()

    monkeypatch.setattr("meme_arsenal.remix_server.save_remix_result", fake_save_remix_result)
    monkeypatch.setattr(store, "_finish_saved_item", fake_finish_saved_item)

    # Measure asynchronous save scheduling, not first-run schema creation.
    with session_scope(settings):
        pass

    try:
        started_at = time.monotonic()
        response = store.save(result.job_id)
        elapsed = time.monotonic() - started_at

        assert elapsed < 0.5
        assert response["status"] == "saving"
        assert response["job"]["status"] == "saving"
        assert started.wait(timeout=5)
        assert store.get(result.job_id).status == "saving"

        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = store.get(result.job_id)
            if current and current.status == "saved":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("Timed out waiting for background save")

        current = store.get(result.job_id)
        assert current is not None
        assert current.saved_item["id"] == 123
        assert finished.wait(timeout=1)
    finally:
        release.set()
        store.shutdown()


def test_save_remix_persists_tweaks_only_in_raw_metadata(monkeypatch, settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        remix_model="gemini-3.1-flash-image",
        embedding_model="gemini-embedding-2",
    )
    tweaks = remix_tweaks_from_payload(
        {
            "analysis_prompt_version": REMIX_TWEAK_ANALYSIS_PROMPT_VERSION,
            "summary": "Lean into captions.",
            "controls": [
                {
                    "id": "caption-energy",
                    "label": "Caption energy",
                    "description": "How much text carries the joke.",
                    "low_label": "visual gag",
                    "high_label": "text-forward",
                    "default_value": 62,
                }
            ],
            "values": {"caption-energy": 72},
            "schizo_level": 1,
            "chronically_online_level": 2,
            "no_text": True,
            "comic_strip": True,
            "anti_slop": True,
        }
    )

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", lambda *_args, **_kwargs: gemini_image_response())
    monkeypatch.setattr(
        "meme_arsenal.embeddings.post_google_model",
        lambda *_args, **_kwargs: {"embedding": {"values": [0.1] * live_settings.embedding_dimensions}},
    )

    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        result = generate_remix(
            session,
            live_settings,
            media_ids[0],
            media_ids[1],
            job_id="save-tweaks-test",
            tweaks=tweaks,
        )
        saved = save_remix_result(session, live_settings, result)
        remix_message = session.scalar(select(TelegramMessage).where(TelegramMessage.chat_id == REMIX_CHAT_ID))
        remix_classification = session.scalar(
            select(Classification).where(Classification.media_item_id == saved["item"]["id"])
        )

    assert saved["item"]["keywords"] == ["deploy", "smug", "remix"]
    assert remix_message.raw_metadata_json["remix_tweaks"]["values"] == {"caption-energy": 72}
    assert remix_message.raw_metadata_json["remix_tweaks"]["schizo_level"] == 1
    assert remix_message.raw_metadata_json["remix_tweaks"]["chronically_online_level"] == 2
    assert remix_message.raw_metadata_json["remix_tweaks"]["no_text"] is True
    assert remix_message.raw_metadata_json["remix_tweaks"]["comic_strip"] is True
    assert remix_message.raw_metadata_json["remix_tweaks"]["anti_slop"] is True
    assert remix_classification.raw_response_json["remix_tweaks"]["values"] == {"caption-energy": 72}
    assert remix_classification.raw_response_json["remix_tweaks"]["schizo_level"] == 1
    assert remix_classification.raw_response_json["remix_tweaks"]["chronically_online_level"] == 2
    assert remix_classification.raw_response_json["remix_tweaks"]["no_text"] is True
    assert remix_classification.raw_response_json["remix_tweaks"]["comic_strip"] is True
    assert remix_classification.raw_response_json["remix_tweaks"]["anti_slop"] is True
