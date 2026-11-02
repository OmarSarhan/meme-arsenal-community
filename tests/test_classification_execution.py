from dataclasses import replace

import pytest

from meme_arsenal import ai, pipeline
from meme_arsenal.db import session_scope
from meme_arsenal.models import Classification
from test_classification_inputs import seed


def forbidden(*args, **kwargs):
    raise AssertionError("unexpected paid/preparation work")


def test_reuse_refresh_and_pre_call_key(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(ai, "extract_ocr_text", lambda paths: calls.append("ocr") or "")
    with session_scope(settings) as session:
        message, item, _ = seed(session, settings)
        row = ai.classify_media_item(session, settings, item)
        key = row.compatibility_key
        assert row.execution_provider == "mock" and key
        assert calls == ["ocr"]
        monkeypatch.setattr(ai, "media_context", forbidden)
        assert ai.classify_media_item(session, settings, item) is row
        assert ai.classification_decision(session, settings, item) == ("reuse", row)
        monkeypatch.undo()
        monkeypatch.setattr(ai, "extract_ocr_text", lambda paths: "")
        message.caption = "changed"
        assert ai.classification_decision(session, settings, item)[0] == "execute"
        expected = ai.classification_compatibility_key(session, settings, item)
        def adapter(config, context):
            message.caption = "changed during call"
            return ai.ClassificationPayload(summary="new")
        monkeypatch.setattr(ai, "classify_with_openai", adapter)
        configured = replace(settings, classification_provider="openai")
        expected = ai.classification_compatibility_key(session, configured, item)
        assert ai.classify_media_item(session, configured, item) is row
        assert row.summary == "new" and row.execution_provider == "openai"
        assert row.compatibility_key == expected != key
        assert ai.classification_decision(session, configured, item)[0] == "execute"


@pytest.mark.parametrize("manual", [False, True])
def test_unknown_and_manual_force(settings, monkeypatch, manual):
    monkeypatch.setattr(ai, "extract_ocr_text", lambda paths: "")
    with session_scope(settings) as session:
        _, item, _ = seed(session, settings)
        old = Classification(media_item_id=item.id, model="manual" if manual else "old",
                             prompt_version="manual-v1" if manual else "old", summary="owner",
                             raw_response_json={"provider": "manual" if manual else "mock"})
        session.add(old)
        session.flush()
        assert ai.classify_media_item(session, settings, item) is None
        report = ai.classify_pending_media(session, settings, limit=1)
        assert report["total_candidates"] == 0
        row = ai.classify_media_item(session, settings, item, force=True)
        assert row is not old and row.compatibility_key
        assert old.summary == "owner" and old.execution_provider is None
        assert ai.classify_media_item(session, settings, item) is row
        with pytest.raises(ValueError, match="reserved"):
            ai.classify_media_item(session, replace(settings, ai_model="manual",
                                   classification_prompt_version="manual-v1"), item, force=True)
        assert old.summary == "owner"


@pytest.mark.parametrize("invalid", [False, True])
def test_failed_refresh_preserves_row(settings, monkeypatch, invalid):
    monkeypatch.setattr(ai, "extract_ocr_text", lambda paths: "")
    with session_scope(settings) as session:
        message, item, _ = seed(session, settings)
        row = ai.classify_media_item(session, settings, item)
        before = row.summary, row.raw_response_json, row.execution_provider, row.compatibility_key
        message.caption = "stale"
        def adapter(context):
            if invalid:
                return ai.ClassificationPayload.model_construct(summary="bad", confidence="invalid")
            raise RuntimeError("provider failed")
        monkeypatch.setattr(ai, "classify_with_mock", adapter)
        with pytest.raises((RuntimeError, ValueError)):
            ai.classify_media_item(session, settings, item)
        assert (row.summary, row.raw_response_json, row.execution_provider, row.compatibility_key) == before


def test_missing_frame_authority(settings, monkeypatch):
    from meme_arsenal.models import MediaKeyframe
    monkeypatch.setattr(ai, "media_context", forbidden)
    with session_scope(settings) as session:
        _, item, path = seed(session, settings, "video")
        session.add(MediaKeyframe(media_item_id=item.id, frame_index=0, timestamp_seconds=0,
                                 path=item.preview_path))
        session.flush()
        assert ai.classify_media_item(session, settings, item) is None


def test_unknown_same_slot_and_missing_evidence_preserve(settings, monkeypatch):
    monkeypatch.setattr(ai, "extract_ocr_text", lambda paths: "")
    with session_scope(settings) as session:
        _, item, _ = seed(session, settings)
        row = Classification(media_item_id=item.id, model=settings.ai_model,
                             prompt_version=settings.classification_prompt_version, summary="legacy")
        session.add(row)
        session.flush()
        assert ai.classify_media_item(session, settings, item) is None
        assert row.summary == "legacy"
        assert ai.classify_media_item(session, settings, item, force=True) is row
        key = row.compatibility_key
        item.original_path = item.preview_path
        item.sha256 = None
        assert ai.classify_media_item(session, settings, item, force=True) is None
        assert row.compatibility_key == key


def test_pipeline_unknown_does_not_pace(settings, monkeypatch):
    with session_scope(settings) as session:
        _, item, _ = seed(session, settings)
        session.add(Classification(media_item_id=item.id, model="legacy", prompt_version="old"))
        media_id = item.id
    class Pacer:
        wait = forbidden
    result = pipeline._classify_one(settings, media_id, Pacer(), False)
    assert result.status == "withheld" and not result.ok


def test_pending_limit_after_unknown_and_current(settings, monkeypatch):
    from datetime import timedelta
    monkeypatch.setattr(ai, "extract_ocr_text", lambda paths: "")
    with session_scope(settings) as session:
        message, oldest, _ = seed(session, settings)
        from meme_arsenal.models import MediaItem, TelegramMessage
        for index in (1, 2):
            newer = TelegramMessage(telegram_message_id=900 + index, chat_id="fixture",
                                    date=message.date + timedelta(days=index), caption="newer")
            session.add(newer)
            session.flush()
            item = MediaItem(telegram_message_id=newer.id, media_type="image",
                             preview_path=oldest.preview_path, processing_status="processed")
            session.add(item)
            session.flush()
            if index == 1:
                ai.classify_media_item(session, settings, item)
            else:
                session.add(Classification(media_item_id=item.id, model="legacy", prompt_version="old"))
        session.flush()
        report = ai.classify_pending_media(session, settings, limit=1)
        assert report["total_candidates"] == report["classified"] == 1
        assert ai.classification_decision(session, settings, oldest)[0] == "reuse"
        report = ai.classify_pending_media(session, settings, limit=1, force=True)
        assert report["classified"] == 1
        assert ai.classification_decision(session, settings, item)[0] == "reuse"


@pytest.mark.parametrize("force", [False, True])
def test_pending_limit_skips_unestablishable_source(settings, monkeypatch, force):
    from datetime import timedelta
    from meme_arsenal.models import MediaItem, MediaKeyframe, TelegramMessage
    monkeypatch.setattr(ai, "extract_ocr_text", lambda paths: "")
    with session_scope(settings) as session:
        message, oldest, path = seed(session, settings)
        newer = TelegramMessage(telegram_message_id=901, chat_id="fixture",
                                date=message.date + timedelta(days=1), caption="newer")
        session.add(newer)
        session.flush()
        item = MediaItem(telegram_message_id=newer.id, media_type="video",
                         preview_path=oldest.preview_path, processing_status="processed")
        session.add(item)
        session.flush()
        session.add(MediaKeyframe(media_item_id=item.id, frame_index=0, timestamp_seconds=0,
                                 path=item.preview_path, sha256=None))
        session.flush()
        assert ai.classification_compatibility_key(session, settings, item) is None
        with monkeypatch.context() as guard:
            guard.setattr(type(path), "open", forbidden)
            guard.setattr(ai, "establish_derivative_hashes", forbidden)
            guard.setattr(ai, "media_context", forbidden)
            from sqlalchemy import event
            event.listen(session, "before_flush", forbidden)
            try:
                assert ai.classification_decision(session, settings, oldest, force)[0] == "execute"
                assert ai.classification_decision(session, settings, item, force)[0] == "unknown"
            finally:
                event.remove(session, "before_flush", forbidden)
        report = ai.classify_pending_media(session, settings, limit=1, force=force)
        assert report["total_candidates"] == report["classified"] == 1
        assert ai.classification_decision(session, settings, oldest)[0] == "reuse"
        assert session.query(Classification).filter_by(media_item_id=item.id).count() == 0


def test_blocked_reuse_and_policy_refresh(settings, monkeypatch):
    configured = replace(settings, classification_provider="google", google_api_key="fixture-key")
    monkeypatch.setattr(ai, "extract_ocr_text", lambda paths: "")
    submissions = []
    def post(config, model, method, payload):
        submissions.append(payload)
        return {"promptFeedback": {"blockReason": "SAFETY"}}
    monkeypatch.setattr(ai, "post_google_model", post)
    with session_scope(settings) as session:
        _, item, _ = seed(session, settings)
        row = ai.classify_media_item(session, configured, item)
        assert ai.classification_is_blocked(row)
        assert len(submissions) == 1
        assert row.raw_response_json["response"]["promptFeedback"]["blockReason"] == "SAFETY"
        monkeypatch.setattr(ai, "media_context", forbidden)
        assert ai.classify_media_item(session, configured, item) is row
        assert len(submissions) == 1
        monkeypatch.setattr(ai, "CLASSIFICATION_INPUT_POLICY", "changed")
        assert ai.classification_decision(session, configured, item)[0] == "execute"
