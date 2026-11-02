from dataclasses import replace
from datetime import datetime, timezone

from meme_arsenal import ai
from meme_arsenal.db import session_scope
from meme_arsenal.media import establish_derivative_hashes, path_sha256, process_media_item
from meme_arsenal.models import MediaItem, MediaKeyframe, TelegramMessage


def seed(session, settings, media_type="image"):
    message = TelegramMessage(telegram_message_id=900, chat_id="fixture", date=datetime.now(timezone.utc), caption="hello")
    session.add(message)
    session.flush()
    path = settings.previews_dir / "input.jpg"
    from PIL import Image
    Image.new("RGB", (8, 8)).save(path)
    stored = path.relative_to(settings.reserve_path).as_posix()
    item = MediaItem(telegram_message_id=message.id, media_type=media_type,
                     preview_path=stored, thumbnail_path=stored, processing_status="processed")
    session.add(item)
    session.flush()
    return message, item, path


def test_unknown_establishment_and_pure_lookup(settings, monkeypatch):
    with session_scope(settings) as session:
        message, item, path = seed(session, settings)
        assert ai.classification_compatibility_key(session, settings, item) is None
        establish_derivative_hashes(settings, item)
        assert item.derivative_hashes_json[item.preview_path] == path_sha256(path)
        session.flush()
        def forbidden(*args, **kwargs):
            raise AssertionError("lookup executed work")
        monkeypatch.setattr(type(path), "open", forbidden)
        monkeypatch.setattr(ai, "extract_ocr_text", forbidden)
        monkeypatch.setattr(ai, "_run_ffmpeg", forbidden)
        monkeypatch.setattr(ai, "_read_image_data_url", forbidden)
        monkeypatch.setattr(ai, "_read_image_inline_data", forbidden)
        monkeypatch.setattr(ai, "_video_inline_context_parts", forbidden)
        from sqlalchemy import event
        event.listen(session, "before_flush", forbidden)
        for provider in ("mock", "openai", "google"):
            assert ai.classification_compatibility_key(session, replace(settings, classification_provider=provider), item)
        key = ai.classification_compatibility_key(session, settings, item)
        assert len(key) == 64
        item.updated_at = datetime.now(timezone.utc)
        assert ai.classification_compatibility_key(session, settings, item) == key
        item.mime_type = "ignored"
        message.raw_metadata_json = {"source": "remix", "remix_context": {"intent": "ignored"}}
        assert ai.classification_compatibility_key(session, settings, item) == key
        message.caption = "changed"
        assert ai.classification_compatibility_key(session, settings, item) != key
        message.caption = "hello"
        monkeypatch.setattr(ai, "CLASSIFICATION_INPUT_POLICY", "next")
        assert ai.classification_compatibility_key(session, settings, item) != key
        item.processing_status = "failed"
        assert ai.classification_compatibility_key(session, settings, item) is None
        event.remove(session, "before_flush", forbidden)


def test_order_caps_and_video_ocr(settings, monkeypatch):
    settings = replace(settings, max_video_keyframes=2)
    with session_scope(settings) as session:
        _, item, path = seed(session, settings, "video")
        establish_derivative_hashes(settings, item)
        for index in range(4):
            frame = path.parent / f"frame-{index}.jpg"
            frame.write_bytes(path.read_bytes())
            session.add(MediaKeyframe(media_item_id=item.id, frame_index=index, timestamp_seconds=index,
                                     path=frame.relative_to(settings.reserve_path).as_posix(), sha256=str(index)*64))
        session.flush()
        inputs = ai.media_context_inputs(session, settings, item)
        assert len(inputs["visual_paths"]) == 2
        assert len(inputs["ocr_paths"]) == 4
        key = ai.classification_compatibility_key(session, settings, item)
        frame = session.query(MediaKeyframe).filter_by(frame_index=3).one()
        frame.sha256 = "f"*64
        assert ai.classification_compatibility_key(session, settings, item) != key
        item.media_type = "image"
        inputs = ai.media_context_inputs(session, settings, item)
        assert inputs["ocr_paths"] == [path, path]
        calls = []
        monkeypatch.setattr(ai, "extract_ocr_text", lambda paths: calls.append(paths) or "ocr")
        assert ai.media_context(session, settings, item)["ocr_text"] == "ocr"
        assert calls == [[path, path]]


def test_provider_sources_and_prompt_schema(settings, monkeypatch):
    with session_scope(settings) as session:
        message, item, path = seed(session, settings, "video")
        establish_derivative_hashes(settings, item)
        item.original_path = item.preview_path
        item.sha256 = path_sha256(path)
        for provider in ("openai", "google"):
            configured = replace(settings, classification_provider=provider)
            key = ai.classification_compatibility_key(session, configured, item)
            assert key
            item.mime_type = "changed"
            assert ai.classification_compatibility_key(session, configured, item) != key
            item.mime_type = None
            message.raw_metadata_json = {"source": "remix", "remix_context": {"intent": "new"}}
            assert ai.classification_compatibility_key(session, configured, item) != key
            message.raw_metadata_json = {}
        google = replace(settings, classification_provider="google", ai_include_video_original=True)
        key = ai.classification_compatibility_key(session, google, item)
        assert ai.classification_compatibility_key(session, replace(google, ai_include_video_original=False), item) != key
        assert ai.classification_compatibility_key(session, replace(google, ai_video_inline_max_mb=1), item) != key
        path.write_bytes(b"different size")
        assert ai.classification_compatibility_key(session, google, item) != key
        key = ai.classification_compatibility_key(session, google, item)
        monkeypatch.setattr(ai, "_classification_prompt", lambda provider: "changed prompt")
        assert ai.classification_compatibility_key(session, google, item) != key
        key = ai.classification_compatibility_key(session, google, item)
        monkeypatch.setattr(ai, "GEMINI_CLASSIFICATION_SCHEMA", {"changed": True})
        assert ai.classification_compatibility_key(session, google, item) != key


def test_processing_refresh_and_folder_guard(settings):
    with session_scope(settings) as session:
        _, item, path = seed(session, settings)
        item.original_path = item.preview_path
        item.sha256 = path_sha256(path)
        item.size_bytes = path.stat().st_size
        item.origin = "folder"
        assert process_media_item(session, settings, item)
        first = dict(item.derivative_hashes_json)
        item.derivative_hashes_json = {key: "0"*64 for key in first}
        assert process_media_item(session, settings, item)
        assert item.derivative_hashes_json == first
        authority = item.sha256, item.size_bytes
        path.write_bytes(b"corrupt")
        assert not process_media_item(session, settings, item)
        assert (item.sha256, item.size_bytes) == authority
        assert ai.classification_compatibility_key(session, settings, item) is None


def test_selected_content_order_and_rendered_preview(settings, monkeypatch):
    configured = replace(settings, classification_provider="openai")
    with session_scope(settings) as session:
        message, item, path = seed(session, settings)
        thumb = path.parent / "thumb.jpg"
        thumb.write_bytes(b"other source")
        item.thumbnail_path = thumb.relative_to(settings.reserve_path).as_posix()
        establish_derivative_hashes(settings, item)
        session.flush()
        key = ai.classification_compatibility_key(session, configured, item)
        assert key
        item.preview_path, item.thumbnail_path = item.thumbnail_path, item.preview_path
        assert ai.classification_compatibility_key(session, configured, item) != key
        item.preview_path, item.thumbnail_path = item.thumbnail_path, item.preview_path
        hashes = dict(item.derivative_hashes_json)
        item.derivative_hashes_json = {**hashes, item.preview_path: "f"*64}
        assert ai.classification_compatibility_key(session, configured, item) != key
        item.derivative_hashes_json = hashes
        monkeypatch.setattr(ai, "link_preview_text", lambda preview: "new rendered preview")
        assert ai.classification_compatibility_key(session, configured, item) != key
        key = ai.classification_compatibility_key(session, configured, item)
        monkeypatch.setattr(ai, "CLASSIFICATION_JSON_SCHEMA", {"new": "schema"})
        assert ai.classification_compatibility_key(session, configured, item) != key
        item.derivative_hashes_json = {}
        assert ai.classification_compatibility_key(session, configured, item) is None
