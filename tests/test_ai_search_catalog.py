from __future__ import annotations

import base64
import json
import pytest
from dataclasses import replace
from datetime import datetime, timezone

from PIL import Image
from sqlalchemy import func, select

from meme_arsenal.ai import (
    GEMINI_CLASSIFICATION_SCHEMA,
    ClassificationPayload,
    classify_pending_media,
    classify_with_google,
    media_context,
)
from meme_arsenal.catalog import export_catalog
from meme_arsenal.config import Settings
from meme_arsenal.db import session_scope
from meme_arsenal.dashboard import build_dashboard
from meme_arsenal.embeddings import embed_media_item, embed_pending_media, embed_text
from meme_arsenal.google_api import GeminiApiError
from meme_arsenal.media import reserve_relative
from meme_arsenal.media import process_pending_media
from meme_arsenal.models import Classification, Curation, Embedding, MediaItem, MediaKeyframe, TelegramMessage
from meme_arsenal.search import export_search_results, search_media
from meme_arsenal.telegram_importer import import_fixture_messages


def prepare_archive(settings, fixture_dir):
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)
        process_pending_media(session, settings)
        classify_pending_media(session, settings)
        embed_pending_media(session, settings)


@pytest.mark.parametrize("provider", ["mock", "openai", "google"])
@pytest.mark.parametrize("override", [False, True])
def test_classification_provider_routing(settings, fixture_dir, monkeypatch, provider, override):
    from meme_arsenal import ai
    from meme_arsenal.remix import remix_available
    calls = []
    payload = ClassificationPayload(summary="fixture")
    def adapter(selected, context):
        calls.append(selected.ai_model)
        return payload
    monkeypatch.setattr(ai, "classify_with_openai", adapter)
    monkeypatch.setattr(ai, "classify_with_google", lambda s, c: (adapter(s, c), {}))
    settings = replace(settings, ai_provider="mock" if override else provider,
                       classification_provider=provider if override else None,
                       remix_provider="google", google_api_key="synthetic",
                       embedding_provider="mock")
    assert remix_available(settings)
    assert ai._effective_ai_delay_seconds(settings) == (4 if provider == "google" else 0)
    assert ai._effective_ai_delay_seconds(replace(settings, ai_request_delay_seconds=0.25)) == 0.25
    with session_scope(settings) as db:
        import_fixture_messages(db, settings, fixture_dir)
        process_pending_media(db, settings)
        report = classify_pending_media(db, replace(settings, ai_request_delay_seconds=0))
        assert report["provider"] == provider
        records = db.scalars(select(Classification)).all()
        assert records and all(r.raw_response_json["provider"] == provider for r in records)
        assert all(r.model == settings.ai_model for r in records)
    assert bool(calls) == (provider != "mock")


def test_classification_schema_accepts_brief_shape():
    payload = ClassificationPayload.model_validate(
        {
            "summary": "Short description of the meme",
            "visible_text": "DEPLOY FAILED",
            "narration": "",
            "language": "en",
            "meme_format": "reaction image",
            "tone": ["sarcastic"],
            "reaction_type": ["failure"],
            "topics": ["software"],
            "keywords": ["deploy failed"],
            "people_or_characters": [],
            "safety_flags": [],
            "search_phrases": ["when the deploy fails"],
            "confidence": 0.82,
        }
    )
    assert payload.keywords == ["deploy failed"]


def test_classification_schema_bounds_and_deduplicates_tags():
    payload = ClassificationPayload.model_validate(
        {
            "topics": ["Deploy", " deploy ", *[f"topic-{index}" for index in range(30)]],
            "keywords": ["x" * 200],
        }
    )

    assert len(payload.topics) == 12
    assert payload.topics[0] == "Deploy"
    assert payload.topics.count("Deploy") == 1
    assert len(payload.keywords[0]) == 120


def test_embedding_batch_preserves_successes_when_a_later_item_fails(monkeypatch, settings, fixture_dir):
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)
        process_pending_media(session, settings)
        classify_pending_media(session, settings)

        calls = 0

        def flaky_embed_media_item(session, settings, media_item, force=False):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("embedding failed")
            return embed_media_item(session, settings, media_item, force=force)

        monkeypatch.setattr("meme_arsenal.embeddings.embed_media_item", flaky_embed_media_item)
        counts = embed_pending_media(session, settings)

    assert counts["embedded"] == 1
    assert counts["failed"] == 1
    with session_scope(settings) as session:
        assert session.scalar(select(func.count()).select_from(Embedding)) == 1


def test_video_media_context_uses_all_keyframes_for_ocr(monkeypatch, settings):
    preview = settings.previews_dir / "video-preview.jpg"
    thumb = settings.thumbnails_dir / "video-thumb.jpg"
    frame_dir = settings.keyframes_dir / "video"
    frame_one = frame_dir / "frame-000.jpg"
    frame_two = frame_dir / "frame-001.jpg"
    frame_three = frame_dir / "frame-002.jpg"
    preview.parent.mkdir(parents=True, exist_ok=True)
    thumb.parent.mkdir(parents=True, exist_ok=True)
    frame_dir.mkdir(parents=True, exist_ok=True)

    for image in [preview, thumb, frame_one, frame_two, frame_three]:
        Image.new("RGB", (24, 18), (10, 20, 30)).save(image, "JPEG")

    captured = {}

    def fake_extract_ocr_text(paths):
        captured["paths"] = [path.name for path in paths]
        return " ".join(path.stem for path in paths)

    monkeypatch.setattr("meme_arsenal.ai.extract_ocr_text", fake_extract_ocr_text)

    with session_scope(settings) as session:
        message = TelegramMessage(
            telegram_message_id=111,
            chat_id="fixture",
            date=datetime(2026, 1, 6, tzinfo=timezone.utc),
            caption="video with OCR chain",
        )
        session.add(message)
        session.flush()
        media_item = MediaItem(
            telegram_message_id=message.id,
            media_type="video",
            mime_type="video/mp4",
            original_path="media/videos/video.mp4",
            preview_path=reserve_relative(settings, preview),
            thumbnail_path=reserve_relative(settings, thumb),
            processing_status="processed",
        )
        session.add(media_item)
        session.flush()
        session.add_all(
            [
                MediaKeyframe(
                    media_item_id=media_item.id,
                    frame_index=0,
                    timestamp_seconds=1.0,
                    path=reserve_relative(settings, frame_one),
                ),
                MediaKeyframe(
                    media_item_id=media_item.id,
                    frame_index=1,
                    timestamp_seconds=2.0,
                    path=reserve_relative(settings, frame_two),
                ),
                MediaKeyframe(
                    media_item_id=media_item.id,
                    frame_index=2,
                    timestamp_seconds=3.0,
                    path=reserve_relative(settings, frame_three),
                ),
            ]
        )
        context = media_context(session, settings, media_item)

    assert captured["paths"] == ["frame-000.jpg", "frame-001.jpg", "frame-002.jpg"]
    assert "frame-000" in context["ocr_text"]
    assert "frame-001" in context["ocr_text"]
    assert "frame-002" in context["ocr_text"]


def test_google_classification_uses_generate_content(monkeypatch, tmp_path):
    captured = {}
    image_path = tmp_path / "meme.png"
    Image.new("RGB", (80, 60), (120, 30, 90)).save(image_path, "PNG")

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
                                        "summary": "Deploy failure reaction meme",
                                        "visible_text": "DEPLOY FAILED",
                                        "language": "en",
                                        "meme_format": "reaction image",
                                        "tone": ["dry"],
                                        "reaction_type": ["failure"],
                                        "topics": ["software"],
                                        "keywords": ["deploy", "failure"],
                                        "people_or_characters": [],
                                        "safety_flags": [],
                                        "search_phrases": ["deploy failed meme"],
                                        "confidence": 0.91,
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr("meme_arsenal.ai.post_google_model", fake_post)
    settings = Settings(
        ai_provider="google",
        ai_model="gemini-3.5-flash",
        google_api_key="test-google-key",
    )

    payload, _raw = classify_with_google(
        settings,
        {
            "caption": "when deploy fails",
            "ocr_text": "DEPLOY FAILED",
            "media_type": "photo",
            "mime_type": "image/png",
            "visual_paths": [image_path],
        },
    )

    assert payload.summary == "Deploy failure reaction meme"
    assert captured["model"] == "gemini-3.5-flash"
    assert captured["method"] == "generateContent"
    generation_config = captured["payload"]["generationConfig"]
    assert generation_config["responseMimeType"] == "application/json"
    assert generation_config["responseSchema"] == GEMINI_CLASSIFICATION_SCHEMA
    assert "responseFormat" not in generation_config
    assert captured["payload"]["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "image/jpeg"


def test_google_classification_retries_without_unprocessable_image(monkeypatch, tmp_path):
    image_path = tmp_path / "odd-source.png"
    Image.new("RGB", (80, 60), (12, 80, 160)).save(image_path, "PNG")
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
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "summary": "Metadata-only fallback",
                                        "visible_text": "DEPLOY FAILED",
                                        "language": "en",
                                        "meme_format": "reaction image",
                                        "tone": ["dry"],
                                        "reaction_type": ["failure"],
                                        "topics": ["software"],
                                        "keywords": ["deploy", "fallback"],
                                        "people_or_characters": [],
                                        "safety_flags": [],
                                        "search_phrases": ["deploy failed fallback"],
                                        "confidence": 0.72,
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr("meme_arsenal.ai.post_google_model", fake_post)
    settings = Settings(
        ai_provider="google",
        ai_model="gemini-3.5-flash",
        google_api_key="test-google-key",
    )

    payload, raw = classify_with_google(
        settings,
        {
            "caption": "when deploy fails",
            "ocr_text": "DEPLOY FAILED",
            "media_type": "photo",
            "mime_type": "image/png",
            "visual_paths": [image_path],
        },
    )

    assert payload.summary == "Metadata-only fallback"
    assert len(calls) == 2
    assert any("inline_data" in part for part in calls[0]["contents"][0]["parts"])
    assert not any("inline_data" in part for part in calls[1]["contents"][0]["parts"])
    assert raw["media_retry_without_inline_data"] is True


def test_google_classification_can_include_small_video(monkeypatch, tmp_path):
    captured = {}
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"small-video-bytes")

    def fake_post(settings, model, method, payload):
        captured["payload"] = payload
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "summary": "Narrated reaction video",
                                        "visible_text": "",
                                        "narration": "A speaker says the build finally passed.",
                                        "language": "en",
                                        "meme_format": "short video meme",
                                        "tone": ["playful"],
                                        "reaction_type": ["reaction"],
                                        "topics": ["software"],
                                        "keywords": ["narration", "reaction"],
                                        "people_or_characters": [],
                                        "safety_flags": [],
                                        "search_phrases": ["narrated video reaction"],
                                        "confidence": 0.84,
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr("meme_arsenal.ai.post_google_model", fake_post)
    settings = Settings(
        ai_provider="google",
        ai_model="gemini-3.5-flash",
        google_api_key="test-google-key",
        ai_video_inline_max_mb=1,
    )

    payload, _raw = classify_with_google(
        settings,
        {
            "caption": "listen to this",
            "ocr_text": "",
            "media_type": "video",
            "mime_type": "video/mp4",
            "visual_paths": [],
            "video_path": video_path,
        },
    )

    parts = captured["payload"]["contents"][0]["parts"]
    assert payload.summary == "Narrated reaction video"
    assert payload.narration == "A speaker says the build finally passed."
    assert any(part.get("inline_data", {}).get("mime_type") == "video/mp4" for part in parts)


def test_google_classification_compresses_oversized_video(monkeypatch, tmp_path):
    captured = {"ffmpeg": []}
    video_path = tmp_path / "large.mp4"
    video_path.write_bytes(b"x" * 2048)

    def fake_run(args, *, check, stdout, stderr, timeout):
        assert timeout == 300.0
        captured["ffmpeg"].append(args)
        output = tmp_path / "compressed-from-ffmpeg.mp4"
        output.write_bytes(b"compressed-video")
        # The production code passes its own output path as the final argument.
        output.replace(args[-1])

    def fake_post(settings, model, method, payload):
        captured["payload"] = payload
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "summary": "Compressed narrated video",
                                        "visible_text": "",
                                        "narration": "Audio survives the compressed video fallback.",
                                        "language": "en",
                                        "meme_format": "short video meme",
                                        "tone": ["playful"],
                                        "reaction_type": ["reaction"],
                                        "topics": ["video"],
                                        "keywords": ["compressed"],
                                        "people_or_characters": [],
                                        "safety_flags": [],
                                        "search_phrases": ["compressed video narration"],
                                        "confidence": 0.8,
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr("meme_arsenal.ai.shutil.which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
    monkeypatch.setattr("meme_arsenal.ai.subprocess.run", fake_run)
    monkeypatch.setattr("meme_arsenal.ai.post_google_model", fake_post)
    settings = Settings(
        ai_provider="google",
        ai_model="gemini-3.5-flash",
        google_api_key="test-google-key",
        ai_video_inline_max_mb=0.001,
    )

    payload, raw = classify_with_google(
        settings,
        {
            "caption": "listen to this",
            "ocr_text": "",
            "media_type": "video",
            "mime_type": "video/mp4",
            "visual_paths": [],
            "video_path": video_path,
        },
    )

    parts = captured["payload"]["contents"][0]["parts"]
    inline_media = [part["inline_data"] for part in parts if "inline_data" in part]
    text_parts = [part["text"] for part in parts if "text" in part]

    assert payload.summary == "Compressed narrated video"
    assert any("compressed video follows" in text for text in text_parts)
    assert inline_media == [
        {
            "mime_type": "video/mp4",
            "data": base64.b64encode(b"compressed-video").decode("ascii"),
        }
    ]
    assert raw["video_inline"]["mode"] == "compressed_3fps_video"
    assert raw["video_inline"]["fps"] == 3
    assert any("fps=3" in arg for call in captured["ffmpeg"] for arg in call)


def test_google_classification_extracts_audio_when_compressed_video_stays_oversized(monkeypatch, tmp_path):
    captured = {"ffmpeg": []}
    video_path = tmp_path / "huge.mp4"
    video_path.write_bytes(b"x" * 4096)

    def fake_run(args, *, check, stdout, stderr, timeout):
        assert timeout == 300.0
        captured["ffmpeg"].append(args)
        output = tmp_path / "ffmpeg-output"
        if str(args[-1]).endswith(".mp4"):
            output.write_bytes(b"v" * 4096)
        else:
            output.write_bytes(b"audio-only")
        output.replace(args[-1])

    def fake_post(settings, model, method, payload):
        captured["payload"] = payload
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "summary": "Audio narrated video",
                                        "visible_text": "",
                                        "narration": "Only the extracted audio fit the request.",
                                        "language": "en",
                                        "meme_format": "short video meme",
                                        "tone": ["plain"],
                                        "reaction_type": ["reaction"],
                                        "topics": ["audio"],
                                        "keywords": ["audio"],
                                        "people_or_characters": [],
                                        "safety_flags": [],
                                        "search_phrases": ["audio-only video narration"],
                                        "confidence": 0.73,
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr("meme_arsenal.ai.shutil.which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
    monkeypatch.setattr("meme_arsenal.ai.subprocess.run", fake_run)
    monkeypatch.setattr("meme_arsenal.ai.post_google_model", fake_post)
    settings = Settings(
        ai_provider="google",
        ai_model="gemini-3.5-flash",
        google_api_key="test-google-key",
        ai_video_inline_max_mb=0.001,
    )

    payload, raw = classify_with_google(
        settings,
        {
            "caption": "listen to this",
            "ocr_text": "",
            "media_type": "video",
            "mime_type": "video/mp4",
            "visual_paths": [],
            "video_path": video_path,
        },
    )

    parts = captured["payload"]["contents"][0]["parts"]
    inline_media = [part["inline_data"] for part in parts if "inline_data" in part]
    text_parts = [part["text"] for part in parts if "text" in part]

    assert payload.summary == "Audio narrated video"
    assert any("Extracted audio follows" in text for text in text_parts)
    assert inline_media == [
        {
            "mime_type": "audio/mp4",
            "data": base64.b64encode(b"audio-only").decode("ascii"),
        }
    ]
    assert raw["video_inline"]["mode"] == "extracted_audio"
    assert any(str(call[-1]).endswith(".m4a") for call in captured["ffmpeg"])


def test_google_block_reason_creates_dashboard_block_marker(monkeypatch, settings, fixture_dir):
    live_settings = replace(
        settings,
        ai_provider="google",
        ai_model="gemini-3.5-flash",
        google_api_key="test-google-key",
        ai_request_delay_seconds=0,
    )

    def fake_post(settings, model, method, payload):
        return {
            "promptFeedback": {"blockReason": "PROHIBITED_CONTENT"},
            "usageMetadata": {"promptTokenCount": 199, "totalTokenCount": 199},
            "modelVersion": model,
            "responseId": "blocked-test",
        }

    monkeypatch.setattr("meme_arsenal.ai.post_google_model", fake_post)

    with session_scope(live_settings) as session:
        import_fixture_messages(session, live_settings, fixture_dir)
        process_pending_media(session, live_settings)
        report = classify_pending_media(session, live_settings, limit=1)
        classification = session.scalar(select(Classification))
        export_catalog(session, live_settings)

    chunk = json.loads((live_settings.catalog_dir / "items-000.json").read_text(encoding="utf-8"))
    blocked_items = [
        item for item in chunk["items"]
        if item["classification_status"] == "blocked"
    ]

    assert report["blocked"] == 1
    assert report["classified"] == 0
    assert report["failed"] == 0
    assert report["blocked_items"][0]["block_reason"] == "PROHIBITED_CONTENT"
    assert classification is not None
    assert classification.raw_response_json["classification_status"] == "blocked"
    assert classification.raw_response_json["classification_block_reason"] == "PROHIBITED_CONTENT"
    assert "gemini_blocked" in classification.safety_flags_json
    assert blocked_items[0]["classification_block_reason"] == "PROHIBITED_CONTENT"


def test_google_embed_text_uses_configured_model_and_dimensions(monkeypatch):
    captured = {}

    def fake_post(settings, model, method, payload):
        captured["model"] = model
        captured["method"] = method
        captured["payload"] = payload
        return {"embedding": {"values": [0.1] * 768}}

    monkeypatch.setattr("meme_arsenal.embeddings.post_google_model", fake_post)
    settings = Settings(
        ai_provider="google",
        embedding_model="gemini-embedding-2",
        embedding_dimensions=768,
        google_api_key="test-google-key",
    )

    values = embed_text(settings, "deploy failed")

    assert values == [0.1] * 768
    assert captured["model"] == "gemini-embedding-2"
    assert captured["method"] == "embedContent"
    assert captured["payload"]["model"] == "models/gemini-embedding-2"
    assert captured["payload"]["outputDimensionality"] == 768


def test_mock_classify_embed_and_search(settings, fixture_dir):
    prepare_archive(settings, fixture_dir)
    with session_scope(settings) as session:
        assert session.scalar(select(func.count()).select_from(Classification)) == 2
        assert session.scalar(select(func.count()).select_from(Embedding)) == 2
        hits = search_media(session, settings, "example memes", top_k=3)
        bundle = export_search_results(session, settings, "deploy fails")

    assert hits
    assert hits[0].lexical_score > 0
    assert bundle.exists()


def test_classify_report_includes_status_metadata(settings, fixture_dir):
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)
        process_pending_media(session, settings)
        report = classify_pending_media(session, settings)

    assert report["classified"] == 2
    assert report["failed"] == 0
    assert report["skipped"] == 0
    assert report["total_candidates"] == 2
    assert report["provider"] == "mock"
    assert report["failed_items"] == []


def test_catalog_export_and_dashboard_fallback(settings, fixture_dir, monkeypatch):
    prepare_archive(settings, fixture_dir)
    with session_scope(settings) as session:
        first_media_id = session.scalar(select(MediaItem.id).order_by(MediaItem.id))
        session.add(
            Curation(
                media_item_id=first_media_id,
                favorite=True,
                manual_tags_json=["ops-meltdown"],
                notes="Pager notes",
            )
        )
        index = export_catalog(session, settings, chunk_size=1)
        search_index = (settings.catalog_dir / "search-index.json").read_text(encoding="utf-8")

    monkeypatch.setattr("meme_arsenal.dashboard.shutil.which", lambda _name: None)
    result = build_dashboard(settings)
    item_chunks = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(settings.catalog_dir.glob("items-*.json"))
    )
    fallback_js = (settings.dashboard_dist_dir / "assets" / "fallback.js").read_text(encoding="utf-8")

    assert index["item_count"] == 2
    assert len(index["chunks"]) == 2
    assert (settings.catalog_dir / "index.json").exists()
    assert "example.com/deploy-fails" in search_index
    assert "ops-meltdown" in search_index
    assert "pager notes" in search_index
    assert "favorite" in search_index
    assert "image meme" in search_index
    assert "Deploy failed reaction" in item_chunks
    assert result["mode"] == "fallback"
    assert 'HIDDEN_MEDIA_TYPES = new Set(["document", "sticker"])' in fallback_js
    assert "filter(item => !HIDDEN_MEDIA_TYPES.has(item.media_type))" in fallback_js
    assert (settings.dashboard_dist_dir / "index.html").exists()
