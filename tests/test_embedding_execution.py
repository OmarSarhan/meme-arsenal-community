from dataclasses import replace

import pytest
from sqlalchemy import select

from meme_arsenal import ai, embeddings as e
from meme_arsenal.db import session_scope
from meme_arsenal.models import Classification, Embedding
from test_classification_inputs import seed


def establish(session, settings, monkeypatch):
    monkeypatch.setattr(ai, "extract_ocr_text", lambda paths: "")
    message, item, _ = seed(session, settings)
    row = ai.classify_media_item(session, settings, item)
    return message, item, row


def test_exact_reuse_and_stale_authority(settings, monkeypatch):
    with session_scope(settings) as db:
        message, item, row = establish(db, settings, monkeypatch)
        original = e.embed_media_item(db, settings, item)
        calls = []
        monkeypatch.setattr(e, "embed_text", lambda s, t: calls.append(t) or [0.5] * s.embedding_dimensions)
        assert e.embed_media_item(db, settings, item) is original
        row.language = "ignored"
        row.people_or_characters_json = ["ignored"]
        row.safety_flags_json = ["retain"]
        assert e.embed_media_item(db, settings, item) is original
        changed = replace(settings, classification_prompt_version="next")
        assert e.embed_media_item(db, changed, item, force=True) is None
        refreshed = ai.classify_media_item(db, changed, item)
        assert refreshed.id != row.id
        assert e.embed_media_item(db, changed, item) is original
        assert calls == []
        monkeypatch.setattr(ai, "classify_with_mock", lambda context: ai.ClassificationPayload(
            summary="forced changed output"))
        assert ai.classify_media_item(db, changed, item, force=True) is refreshed
        assert e.embed_media_item(db, changed, item) is original
        assert len(calls) == 1
        message.caption = "stale source"
        assert e.embed_media_item(db, changed, item, force=True) is None


@pytest.mark.parametrize("field,value", [("embedding_provider", "google"), ("embedding_model", "next"), ("embedding_dimensions", 3)])
def test_identity_refresh(settings, monkeypatch, field, value):
    with session_scope(settings) as db:
        _, item, _ = establish(db, settings, monkeypatch)
        original = e.embed_media_item(db, settings, item)
        calls = []
        monkeypatch.setattr(e, "embed_text", lambda s, t: calls.append(t) or [1] * s.embedding_dimensions)
        changed = replace(settings, **{field: value})
        result = e.embed_media_item(db, changed, item)
        assert len(calls) == 1
        assert result.execution_provider == changed.effective_embedding_provider
        assert result.embedding_dimensions == changed.embedding_dimensions
        if field == "embedding_model":
            assert result is not original


def test_manual_precedence_and_wrong_supplied(settings, monkeypatch):
    with session_scope(settings) as db:
        message, item, ai_row = establish(db, settings, monkeypatch)
        manual = Classification(media_item_id=item.id, model="manual", prompt_version="manual-v1",
                                summary="owner", raw_response_json={"provider": "manual"},
                                safety_flags_json=["retain"])
        db.add(manual)
        db.flush()
        message.caption = "changed policy input"
        assert e.select_embedding_input(db, settings, item) is manual
        assert e.embed_media_item(db, settings, item, classification=ai_row, force=True) is None
        other = Classification(media_item_id=item.id + 1, model="manual", prompt_version="manual-v1")
        assert e.embed_media_item(db, settings, item, classification=other) is None
        assert e.embed_media_item(db, settings, item, classification=manual)
        assert manual.execution_provider is None and manual.safety_flags_json == ["retain"]


def test_unknown_inputs_and_embedding_slots(settings, monkeypatch):
    with session_scope(settings) as db:
        _, item, _ = seed(db, settings)
        legacy = Classification(media_item_id=item.id, model=settings.ai_model,
                                prompt_version=settings.classification_prompt_version,
                                raw_response_json={"provider": "remix"})
        db.add(legacy)
        db.flush()
        assert e.embed_media_item(db, settings, item, force=True, classification=legacy) is None
        monkeypatch.setattr(ai, "extract_ocr_text", lambda paths: "")
        ai.classify_media_item(db, settings, item, force=True)
        old = Embedding(media_item_id=item.id, embedding_model="legacy", embedding_source="classification",
                        embedding_dimensions=2, embedding_json=[1, 2])
        db.add(old)
        db.flush()
        assert e.embed_media_item(db, settings, item) is None
        assert e.embed_pending_media(db, settings, limit=1)["embedded"] == 0
        assert e.embed_media_item(db, settings, item, force=True)
        assert old.execution_provider is None and old.source_text_hash is None


@pytest.mark.parametrize("bad", [[], [True], ["1"], [float("nan")], [float("inf")], [1, 2]])
def test_vector_validation_and_failure_preservation(settings, monkeypatch, bad):
    with pytest.raises(ValueError):
        e.validate_embedding_vector(bad, 1)
    with session_scope(settings) as db:
        _, item, _ = establish(db, settings, monkeypatch)
        row = e.embed_media_item(db, settings, item)
        before = row.embedding_json[:], row.source_text_hash, row.execution_provider
        monkeypatch.setattr(e, "embed_text", lambda s, t: bad)
        with pytest.raises(ValueError):
            e.embed_media_item(db, settings, item, force=True)
        assert (row.embedding_json, row.source_text_hash, row.execution_provider) == before
        def failure(*args):
            raise RuntimeError("provider failed")
        monkeypatch.setattr(e, "embed_text", failure)
        with pytest.raises(RuntimeError):
            e.embed_media_item(db, settings, item, force=True)
        assert (row.embedding_json, row.source_text_hash, row.execution_provider) == before


def test_cosine():
    assert e.cosine_similarity([0, 0], [1, 2]) == 0
    assert e.cosine_similarity([1, 2], [1, 2]) == pytest.approx(1)
    for left, right in [([], []), ([1], [1, 2]), ([True], [1])]:
        with pytest.raises(ValueError):
            e.cosine_similarity(left, right)


@pytest.mark.parametrize("bad", [[True], ["0.1"], [float("nan")], [float("inf")], []])
def test_provider_rejects_raw_values(settings, monkeypatch, bad):
    monkeypatch.setattr(e, "post_google_model", lambda *a: {"embedding": {"values": bad}})
    with pytest.raises(ValueError):
        e.embed_text(replace(settings, embedding_provider="google", embedding_dimensions=1), "query")


def test_local_maintenance_selects_authority_and_keeps_saved_item(settings, monkeypatch):
    from meme_arsenal import remix_server
    from meme_arsenal.models import MediaItem
    with session_scope(settings) as db:
        _, item, row = establish(db, settings, monkeypatch)
        item_id = item.id
        db.add(Classification(media_item_id=item_id, model="old", prompt_version="old",
                              raw_response_json={"provider": "remix"}))
    monkeypatch.setattr(remix_server, "export_catalog", lambda *a: None)
    store = remix_server.RemixJobStore(settings)
    try:
        store._finish_saved_item("saved", item_id)
        with session_scope(settings) as db:
            assert db.scalar(select(Embedding)).source_text_hash
        monkeypatch.setattr(e, "embed_text", lambda *a: (_ for _ in ()).throw(RuntimeError("failed")))
        store._finish_saved_item("saved", item_id)
        with session_scope(settings) as db:
            assert db.get(MediaItem, item_id)
            assert db.scalar(select(Embedding)).source_text_hash
    finally:
        store.shutdown()


def test_pending_filters_before_limit(settings, fixture_dir, monkeypatch):
    from meme_arsenal.telegram_importer import import_fixture_messages
    from meme_arsenal.media import process_pending_media
    with session_scope(settings) as db:
        import_fixture_messages(db, settings, fixture_dir)
        process_pending_media(db, settings)
        ai.classify_pending_media(db, settings)
        rows = db.scalars(select(Classification).order_by(Classification.media_item_id)).all()
        # Newest item has unknown evidence; duplicate historical C cannot consume limit.
        rows[-1].compatibility_key = None
        db.add(Classification(media_item_id=rows[-1].media_item_id, model="historical", prompt_version="old"))
        db.flush()
        assert e.embed_pending_media(db, settings, limit=1)["embedded"] == 1
        assert e.embed_pending_media(db, settings, limit=1)["embedded"] == 0
