from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import time

from PIL import Image
from sqlalchemy import func, select
from telethon import types

from meme_arsenal.catalog import catalog_item_payload
from meme_arsenal.db import session_scope
from meme_arsenal.media import (
    _video_keyframe_timestamps,
    browser_playback_path,
    ensure_browser_playback,
    media_needs_processing,
    process_media_item,
    process_pending_media,
    reserve_relative,
)
from meme_arsenal.models import MediaItem, MediaKeyframe, TelegramMessage
from meme_arsenal.telegram_importer import (
    _telegram_post_url_from_message,
    _telegram_post_url_from_metadata,
    backfill_telegram_post_urls,
    import_fixture_messages,
    import_saved_messages_async,
)


def test_fixture_import_is_incremental(settings, fixture_dir):
    with session_scope(settings) as session:
        first = import_fixture_messages(session, settings, fixture_dir)
        second = import_fixture_messages(session, settings, fixture_dir)
        message_count = session.scalar(select(func.count()).select_from(TelegramMessage))
        media_count = session.scalar(select(func.count()).select_from(MediaItem))

    assert first["messages"] == 2
    assert first["link_previews"] == 1
    assert second["skipped"] == 2
    assert message_count == 2
    assert media_count == 2


def test_fixture_import_limit_walks_newest_unimported_first(settings, fixture_dir):
    with session_scope(settings) as session:
        first = import_fixture_messages(session, settings, fixture_dir, limit=1)
        second = import_fixture_messages(session, settings, fixture_dir, limit=1)

    assert first["telegram_message_ids"] == [102]
    assert second["telegram_message_ids"] == [101]
    assert second["skipped"] == 1


def test_recent_saved_message_import_does_not_scan_historical_messages(settings, monkeypatch):
    scan_kwargs: list[dict[str, int]] = []

    class FakeTelegramClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self):
            pass

        async def disconnect(self):
            pass

        async def iter_messages(self, _chat, **kwargs):
            scan_kwargs.append(kwargs)
            if False:
                yield None

    monkeypatch.setattr("telethon.TelegramClient", FakeTelegramClient)
    live_settings = replace(settings, telegram_api_id="123", telegram_api_hash="test-hash")
    with session_scope(live_settings) as session:
        session.add(
            TelegramMessage(
                telegram_message_id=100,
                chat_id="me",
                date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                caption="existing item",
            )
        )
        session.flush()

        report = asyncio.run(
            import_saved_messages_async(
                session,
                live_settings,
                limit=100,
                backfill_older=False,
            )
        )

    assert report["media_item_ids"] == []
    assert scan_kwargs == [{"min_id": 100, "limit": 100, "reverse": True}]


def test_fixture_link_preview_metadata_is_persisted(settings, fixture_dir):
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)
        message = session.scalar(
            select(TelegramMessage).where(TelegramMessage.telegram_message_id == 101)
        )

    assert message is not None
    assert message.raw_metadata_json["link_preview"]["url"] == "https://example.com/deploy-fails"
    assert message.raw_metadata_json["link_preview"]["title"] == "Deploy failed reaction"


def test_fixture_telegram_post_url_is_persisted_and_exported(settings, fixture_dir):
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)
        message = session.scalar(
            select(TelegramMessage).where(TelegramMessage.telegram_message_id == 101)
        )
        media_item = session.scalar(select(MediaItem).where(MediaItem.telegram_message_id == message.id))
        payload = catalog_item_payload(session, settings, media_item.id)

    assert message.telegram_post_url == "https://t.me/examplememes/101"
    assert message.raw_metadata_json["telegram_post_url"] == "https://t.me/examplememes/101"
    assert payload["telegram_post_url"] == "https://t.me/examplememes/101"


def test_telegram_post_url_can_be_derived_from_public_chat():
    raw_message = type(
        "RawMessage",
        (),
        {
            "id": 55,
            "peer_id": types.PeerChannel(channel_id=777),
            "chat": type("Chat", (), {"username": "examplememes"})(),
            "forward": None,
        },
    )()

    assert _telegram_post_url_from_message(raw_message) == "https://t.me/examplememes/55"


def test_telegram_post_url_can_be_derived_from_forward_metadata():
    metadata = {
        "peer_id": {"_": "PeerUser", "user_id": 10},
        "fwd_from": {
            "from_id": {"_": "PeerChannel", "channel_id": 987654},
            "channel_post": 321,
        },
    }

    assert _telegram_post_url_from_metadata(metadata, message_id=12) == "https://t.me/c/987654/321"


def test_telegram_post_url_backfill_updates_existing_messages(settings):
    with session_scope(settings) as session:
        message = TelegramMessage(
            telegram_message_id=12,
            chat_id="me",
            caption="forwarded meme",
            raw_metadata_json={
                "peer_id": {"_": "PeerUser", "user_id": 10},
                "fwd_from": {
                    "from_id": {"_": "PeerChannel", "channel_id": 987654},
                    "channel_post": 321,
                },
            },
        )
        session.add(message)
        session.flush()

        counts = backfill_telegram_post_urls(session)

    assert counts == {"checked": 1, "backfilled": 1}
    assert message.telegram_post_url == "https://t.me/c/987654/321"
    assert message.raw_metadata_json["telegram_post_url"] == "https://t.me/c/987654/321"


def test_fixture_import_logs_skipped_preview_url(settings, tmp_path):
    fixture_dir = tmp_path / "link-only"
    fixture_dir.mkdir()
    (fixture_dir / "manifest.json").write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "telegram_message_id": 999,
                        "caption": "link-only post",
                        "date": "2026-01-03T00:00:00+00:00",
                        "link_preview": {
                            "url": "https://example.com/missing-preview",
                            "title": "Missing preview media",
                            "has_preview_media": True,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with session_scope(settings) as session:
        report = import_fixture_messages(session, settings, fixture_dir)

    assert report["skipped"] == 1
    assert report["skipped_items"][0]["urls"] == ["https://example.com/missing-preview"]


def test_media_processing_creates_previews_and_hashes(settings, fixture_dir):
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)
        counts = process_pending_media(session, settings)
        media_items = session.scalars(select(MediaItem)).all()

    assert counts["processed"] == 2
    assert all(item.processing_status == "processed" for item in media_items)
    assert all(item.thumbnail_path for item in media_items)
    assert all((settings.reserve_path / item.thumbnail_path).exists() for item in media_items)
    assert all(item.sha256 for item in media_items)
    assert all(item.duplicate_group_id for item in media_items)


def test_video_keyframe_timestamps_are_evenly_spaced():
    assert _video_keyframe_timestamps(4, 20.0) == [4.0, 8.0, 12.0, 16.0]
    assert _video_keyframe_timestamps(1, 20.0) == [10.0]
    assert _video_keyframe_timestamps(3, None) == [0.0, 2.0, 4.0]


def test_browser_playback_is_versioned_mobile_baseline(settings, monkeypatch):
    source = settings.originals_dir / "source.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source-video")
    commands = []

    def fake_run_ffmpeg(command):
        commands.append(command)
        Path(command[-1]).write_bytes(b"mobile-playback")

    monkeypatch.setattr("meme_arsenal.media.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("meme_arsenal.media._run_ffmpeg", fake_run_ffmpeg)

    expected = settings.video_playback_dir / "media-00000042-mobile-v2.mp4"
    assert browser_playback_path(settings, 42) == expected
    assert ensure_browser_playback(settings, 42, source) == expected
    assert ensure_browser_playback(settings, 42, source) == expected

    assert len(commands) == 1
    command = commands[0]
    assert command[command.index("-profile:v") + 1] == "baseline"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert command[command.index("-profile:a") + 1] == "aac_low"
    assert command[command.index("-ar") + 1] == "48000"
    assert command[command.index("-movflags") + 1] == "+faststart"
    assert "-c" not in command


def test_browser_playback_serializes_concurrent_transcodes(settings, monkeypatch):
    source = settings.originals_dir / "concurrent-source.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source-video")
    commands: list[list[str]] = []

    def fake_run_ffmpeg(command):
        commands.append(command)
        time.sleep(0.1)
        Path(command[-1]).write_bytes(b"mobile-playback")

    monkeypatch.setattr("meme_arsenal.media.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("meme_arsenal.media._run_ffmpeg", fake_run_ffmpeg)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outputs = list(executor.map(lambda _index: ensure_browser_playback(settings, 99, source), range(2)))

    assert outputs == [browser_playback_path(settings, 99)] * 2
    assert len(commands) == 1


def test_video_processing_uses_generated_thumbnail_for_dashboard(settings, monkeypatch):
    playback_calls = []

    def fake_video_metadata(source):
        return 320, 180, 9.2

    def fake_video_keyframes(settings, media_item, source):
        preview = settings.previews_dir / f"media-{media_item.id:08d}.jpg"
        frame = settings.keyframes_dir / f"media-{media_item.id:08d}" / "frame-000.jpg"
        preview.parent.mkdir(parents=True, exist_ok=True)
        frame.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (320, 180), (20, 90, 140)).save(preview, "JPEG")
        Image.new("RGB", (320, 180), (80, 20, 140)).save(frame, "JPEG")
        return preview, [(frame, 4.6)]

    def fake_video_hover_preview(settings, media_item, source, duration_seconds):
        hover_preview = settings.video_previews_dir / f"media-{media_item.id:08d}.mp4"
        hover_preview.parent.mkdir(parents=True, exist_ok=True)
        hover_preview.write_bytes(b"fake-preview")
        return hover_preview

    def fake_browser_playback(settings, media_item_id, source):
        playback_calls.append((media_item_id, source))
        playback = settings.video_playback_dir / f"media-{media_item_id:08d}-mobile-v2.mp4"
        playback.parent.mkdir(parents=True, exist_ok=True)
        playback.write_bytes(b"fake-playback")
        return playback

    monkeypatch.setattr("meme_arsenal.media._video_metadata", fake_video_metadata)
    monkeypatch.setattr("meme_arsenal.media._video_keyframes", fake_video_keyframes)
    monkeypatch.setattr("meme_arsenal.media._video_hover_preview", fake_video_hover_preview)
    monkeypatch.setattr("meme_arsenal.media.ensure_browser_playback", fake_browser_playback)
    source = settings.originals_dir / "clip.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"fake-video")

    with session_scope(settings) as session:
        message = TelegramMessage(
            telegram_message_id=777,
            chat_id="fixture",
            date=datetime(2026, 1, 4, tzinfo=timezone.utc),
            caption="video meme",
        )
        session.add(message)
        session.flush()
        media_item = MediaItem(
            telegram_message_id=message.id,
            media_type="video",
            mime_type="video/mp4",
            original_path=reserve_relative(settings, source),
            processing_status="pending",
        )
        session.add(media_item)
        session.flush()
        media_item_id = media_item.id

        assert process_media_item(session, settings, media_item)
        keyframe_count = session.scalar(select(func.count()).select_from(MediaKeyframe))
        keyframe_timestamp = session.scalar(select(MediaKeyframe.timestamp_seconds))
        payload = catalog_item_payload(session, settings, media_item_id)

    assert media_item.thumbnail_path is not None
    assert media_item.thumbnail_path.startswith("media/thumbnails/")
    assert media_item.preview_path is not None
    assert media_item.preview_path.startswith("media/previews/")
    assert media_item.hover_preview_path is not None
    assert media_item.hover_preview_path.startswith("media/video-previews/")
    assert media_item.width == 320
    assert media_item.height == 180
    assert media_item.duration_seconds == 9.2
    assert (settings.reserve_path / media_item.thumbnail_path).exists()
    assert (settings.reserve_path / media_item.hover_preview_path).exists()
    assert playback_calls == [(media_item_id, source.resolve())]
    assert (settings.video_playback_dir / f"media-{media_item_id:08d}-mobile-v2.mp4").exists()
    assert keyframe_count == 1
    assert keyframe_timestamp == 4.6
    assert payload["hover_preview_path"] is not None
    assert payload["hover_preview_path"].endswith("media/video-previews/media-00000001.mp4")
    assert payload["duration_seconds"] == 9.2
    assert payload["width"] == 320
    assert payload["height"] == 180


def test_processed_video_missing_thumbnail_is_repaired(settings, monkeypatch):
    def fake_video_metadata(source):
        return 320, 180, 7.0

    def fake_video_keyframes(settings, media_item, source):
        preview = settings.previews_dir / f"media-{media_item.id:08d}.jpg"
        preview.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (320, 180), (20, 90, 140)).save(preview, "JPEG")
        return preview, []

    def fake_video_hover_preview(settings, media_item, source, duration_seconds):
        hover_preview = settings.video_previews_dir / f"media-{media_item.id:08d}.mp4"
        hover_preview.parent.mkdir(parents=True, exist_ok=True)
        hover_preview.write_bytes(b"fake-preview")
        return hover_preview

    monkeypatch.setattr("meme_arsenal.media._video_metadata", fake_video_metadata)
    monkeypatch.setattr("meme_arsenal.media._video_keyframes", fake_video_keyframes)
    monkeypatch.setattr("meme_arsenal.media._video_hover_preview", fake_video_hover_preview)
    source = settings.originals_dir / "clip.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"fake-video")

    with session_scope(settings) as session:
        message = TelegramMessage(
            telegram_message_id=778,
            chat_id="fixture",
            date=datetime(2026, 1, 5, tzinfo=timezone.utc),
            caption="processed video missing thumbnail",
        )
        session.add(message)
        session.flush()
        media_item = MediaItem(
            telegram_message_id=message.id,
            media_type="video",
            mime_type="video/mp4",
            original_path=reserve_relative(settings, source),
            processing_status="processed",
        )
        session.add(media_item)
        session.flush()

        assert media_needs_processing(media_item)
        counts = process_pending_media(session, settings)

    assert counts["processed"] == 1
    assert media_item.thumbnail_path is not None
    assert media_item.hover_preview_path is not None
    assert media_item.duration_seconds == 7.0


def test_processed_video_missing_hover_preview_is_backfilled(settings, monkeypatch):
    def fake_video_metadata(source):
        return 640, 360, 12.0

    def fake_video_keyframes(settings, media_item, source):
        preview = settings.previews_dir / f"media-{media_item.id:08d}.jpg"
        preview.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (640, 360), (20, 90, 140)).save(preview, "JPEG")
        return preview, []

    def fake_video_hover_preview(settings, media_item, source, duration_seconds):
        hover_preview = settings.video_previews_dir / f"media-{media_item.id:08d}.mp4"
        hover_preview.parent.mkdir(parents=True, exist_ok=True)
        hover_preview.write_bytes(b"fake-preview")
        return hover_preview

    monkeypatch.setattr("meme_arsenal.media._video_metadata", fake_video_metadata)
    monkeypatch.setattr("meme_arsenal.media._video_keyframes", fake_video_keyframes)
    monkeypatch.setattr("meme_arsenal.media._video_hover_preview", fake_video_hover_preview)
    source = settings.originals_dir / "clip.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"fake-video")

    with session_scope(settings) as session:
        message = TelegramMessage(
            telegram_message_id=779,
            chat_id="fixture",
            date=datetime(2026, 1, 6, tzinfo=timezone.utc),
            caption="processed video missing hover preview",
        )
        session.add(message)
        session.flush()
        media_item = MediaItem(
            telegram_message_id=message.id,
            media_type="video",
            mime_type="video/mp4",
            original_path=reserve_relative(settings, source),
            thumbnail_path="media/thumbnails/existing.jpg",
            preview_path="media/previews/existing.jpg",
            processing_status="processed",
        )
        session.add(media_item)
        session.flush()

        assert media_needs_processing(media_item)
        counts = process_pending_media(session, settings)

    assert counts["processed"] == 1
    assert media_item.hover_preview_path is not None
    assert media_item.width == 640
    assert media_item.height == 360
    assert media_item.duration_seconds == 12.0


def test_processed_video_with_too_few_keyframes_is_backfilled(settings, monkeypatch):
    settings = replace(settings, max_video_keyframes=2)

    def fake_video_metadata(source):
        return 640, 360, 18.0

    def fake_video_keyframes(settings, media_item, source):
        preview = settings.previews_dir / f"media-{media_item.id:08d}.jpg"
        first_frame = settings.keyframes_dir / f"media-{media_item.id:08d}" / "frame-000.jpg"
        second_frame = settings.keyframes_dir / f"media-{media_item.id:08d}" / "frame-001.jpg"
        preview.parent.mkdir(parents=True, exist_ok=True)
        first_frame.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (640, 360), (20, 90, 140)).save(preview, "JPEG")
        Image.new("RGB", (640, 360), (80, 20, 140)).save(first_frame, "JPEG")
        Image.new("RGB", (640, 360), (140, 90, 20)).save(second_frame, "JPEG")
        return preview, [(first_frame, 6.0), (second_frame, 12.0)]

    def fake_video_hover_preview(settings, media_item, source, duration_seconds):
        hover_preview = settings.video_previews_dir / f"media-{media_item.id:08d}.mp4"
        hover_preview.parent.mkdir(parents=True, exist_ok=True)
        hover_preview.write_bytes(b"fake-preview")
        return hover_preview

    monkeypatch.setattr("meme_arsenal.media._video_metadata", fake_video_metadata)
    monkeypatch.setattr("meme_arsenal.media._video_keyframes", fake_video_keyframes)
    monkeypatch.setattr("meme_arsenal.media._video_hover_preview", fake_video_hover_preview)
    source = settings.originals_dir / "clip.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"fake-video")

    with session_scope(settings) as session:
        message = TelegramMessage(
            telegram_message_id=780,
            chat_id="fixture",
            date=datetime(2026, 1, 7, tzinfo=timezone.utc),
            caption="processed video missing richer keyframes",
        )
        session.add(message)
        session.flush()
        media_item = MediaItem(
            telegram_message_id=message.id,
            media_type="video",
            mime_type="video/mp4",
            original_path=reserve_relative(settings, source),
            thumbnail_path="media/thumbnails/existing.jpg",
            preview_path="media/previews/existing.jpg",
            hover_preview_path="media/video-previews/existing.mp4",
            width=640,
            height=360,
            duration_seconds=18.0,
            processing_status="processed",
        )
        session.add(media_item)
        session.flush()
        old_frame = settings.keyframes_dir / "old" / "frame-000.jpg"
        old_frame.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (640, 360), (1, 2, 3)).save(old_frame, "JPEG")
        session.add(
            MediaKeyframe(
                media_item_id=media_item.id,
                frame_index=0,
                timestamp_seconds=0,
                path=reserve_relative(settings, old_frame),
            )
        )
        counts = process_pending_media(session, settings)
        timestamps = session.scalars(
            select(MediaKeyframe.timestamp_seconds).order_by(MediaKeyframe.frame_index)
        ).all()

    assert counts["processed"] == 1
    assert timestamps == [6.0, 12.0]


def test_catalog_uses_existing_video_hover_preview_when_db_path_is_missing(settings):
    source = settings.originals_dir / "clip.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"fake-video")

    with session_scope(settings) as session:
        message = TelegramMessage(
            telegram_message_id=780,
            chat_id="fixture",
            date=datetime(2026, 1, 7, tzinfo=timezone.utc),
            caption="video with already generated hover preview",
        )
        session.add(message)
        session.flush()
        media_item = MediaItem(
            telegram_message_id=message.id,
            media_type="video",
            mime_type="video/mp4",
            original_path=reserve_relative(settings, source),
            thumbnail_path="media/thumbnails/existing.jpg",
            preview_path="media/previews/existing.jpg",
            hover_preview_path=None,
            processing_status="processed",
        )
        session.add(media_item)
        session.flush()

        hover_preview = settings.video_previews_dir / f"media-{media_item.id:08d}.mp4"
        hover_preview.parent.mkdir(parents=True, exist_ok=True)
        hover_preview.write_bytes(b"fake-hover-preview")
        payload = catalog_item_payload(session, settings, media_item.id)

    assert payload["hover_preview_path"] is not None
    assert payload["hover_preview_path"].endswith("media/video-previews/media-00000001.mp4")
