from __future__ import annotations

from sqlalchemy import func, select

from meme_arsenal.db import session_scope
from meme_arsenal.models import Classification, Embedding, MediaItem, TelegramMessage
from meme_arsenal.pipeline import run_media_pipeline
from meme_arsenal.telegram_importer import import_fixture_messages


def test_pipeline_limit_selects_newest_pending_message(settings, fixture_dir) -> None:
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)

    report = run_media_pipeline(
        settings,
        limit=1,
        process_workers=1,
        classify_workers=1,
        embed_workers=1,
    )

    with session_scope(settings) as session:
        classified_message_ids = session.scalars(
            select(TelegramMessage.telegram_message_id)
            .join(MediaItem)
            .join(Classification)
            .order_by(TelegramMessage.telegram_message_id)
        ).all()
        embedding_count = session.scalar(select(func.count()).select_from(Embedding))

    assert report["selected_media_items"] == 1
    assert report["processed"] == 1
    assert report["classified"] == 1
    assert report["embedded"] == 1
    assert classified_message_ids == [102]
    assert embedding_count == 1


def test_pipeline_keeps_local_processing_running_while_ai_is_paused(settings, fixture_dir) -> None:
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)

    report = run_media_pipeline(
        settings,
        process_workers=1,
        classify_workers=1,
        embed_workers=1,
        ai_paused=True,
    )

    with session_scope(settings) as session:
        classification_count = session.scalar(select(func.count()).select_from(Classification))
        embedding_count = session.scalar(select(func.count()).select_from(Embedding))

    assert report["selected_media_items"] == 2
    assert report["processed"] == 2
    assert report["classified"] == 0
    assert report["classify_skipped"] == 2
    assert report["embedded"] == 0
    assert report["embed_skipped"] == 2
    assert classification_count == 0
    assert embedding_count == 0


def test_online_pipeline_can_skip_unused_embeddings_without_reselecting_items(settings, fixture_dir) -> None:
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)

    first = run_media_pipeline(
        settings,
        process_workers=1,
        classify_workers=1,
        embed_workers=1,
        embed_enabled=False,
    )
    second = run_media_pipeline(
        settings,
        process_workers=1,
        classify_workers=1,
        embed_workers=1,
        embed_enabled=False,
    )

    assert first["classified"] == 2
    assert first["embedded"] == 0
    assert first["embed_skipped"] == 2
    assert second["selected_media_items"] == 0
    with session_scope(settings) as session:
        assert session.scalar(select(func.count()).select_from(Embedding)) == 0
