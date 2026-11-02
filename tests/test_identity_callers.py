from dataclasses import replace

import pytest
from sqlalchemy import select

from meme_arsenal import ai, embeddings as e, pipeline, search
from meme_arsenal.catalog import _latest_classification
from meme_arsenal.db import session_scope
from meme_arsenal.models import Classification, Embedding
from test_embedding_execution import establish


def test_pending_identity_and_stage_reuse(settings, monkeypatch):
    with session_scope(settings) as db:
        _, item, _ = establish(db, settings, monkeypatch)
        # Keep the derivative distinct from the original, as imported media does.
        from meme_arsenal.media import path_sha256, reserve_absolute
        original = settings.reserve_path / "original-fixture.jpg"
        original.write_bytes(reserve_absolute(settings, item.preview_path).read_bytes())
        item.original_path = original.relative_to(settings.reserve_path).as_posix()
        item.sha256 = path_sha256(original)
        db.flush()
        item_id = item.id
        e.embed_media_item(db, settings, item)
        assert pipeline.select_pipeline_media_ids(db, settings) == []
        assert pipeline.select_pipeline_media_ids(db, settings, media_ids=[]) == []
        assert pipeline.select_pipeline_media_ids(db, settings, media_ids=[item_id]) == [item_id]
        for changed in [replace(settings, classification_prompt_version="next"),
                        replace(settings, classification_provider="google"),
                        replace(settings, embedding_provider="google"),
                        replace(settings, embedding_model="next"),
                        replace(settings, embedding_dimensions=3)]:
            assert pipeline.select_pipeline_media_ids(db, changed, limit=1) == [item_id]
        assert pipeline.select_pipeline_media_ids(db, replace(settings, embedding_model="next"),
                                                   include_embeddings=False) == []
    assert pipeline._embed_one(settings, item_id, False).status == "skipped"


def test_portable_filtering_and_query_boundary(settings, monkeypatch):
    with session_scope(settings) as db:
        _, item, _ = establish(db, settings, monkeypatch)
        row = e.embed_media_item(db, settings, item)
        row.embedding_vector = None
        vector = row.embedding_json[:]
        assert item.id in search._semantic_scores_portable(db, settings, vector, 1)
        for field, value in [("embedding_json", [1]),
                             ("embedding_json", [True] * settings.embedding_dimensions),
                             ("embedding_json", [float("nan")] * settings.embedding_dimensions),
                             ("embedding_dimensions", settings.embedding_dimensions + 1),
                             ("execution_provider", None),
                             ("source_text_hash", "0" * 64)]:
            old = getattr(row, field)
            setattr(row, field, value)
            assert search._semantic_scores_portable(db, settings, vector, 1) == {}
            setattr(row, field, old)
        monkeypatch.setattr(search, "embed_text", lambda *a: [True] * settings.embedding_dimensions)
        with pytest.raises(ValueError):
            search.search_media(db, settings, "query")
        def failed(*a):
            raise RuntimeError("query provider failed")
        monkeypatch.setattr(search, "embed_text", failed)
        with pytest.raises(RuntimeError, match="query provider failed"):
            search.search_media(db, settings, "query")


def test_portable_top_k_filters_before_deduplication_and_stable_ties(settings, fixture_dir, monkeypatch):
    from meme_arsenal.media import process_pending_media
    from meme_arsenal.models import MediaItem
    from meme_arsenal.telegram_importer import import_fixture_messages
    settings = replace(settings, embedding_dimensions=2)
    monkeypatch.setattr(ai, "extract_ocr_text", lambda *a: "")
    monkeypatch.setattr(e, "embed_text", lambda *a: [1.0, 0.0])
    monkeypatch.setattr(search, "embed_text", lambda *a: [1.0, 0.0])
    with session_scope(settings) as db:
        import_fixture_messages(db, settings, fixture_dir)
        process_pending_media(db, settings)
        ai.classify_pending_media(db, settings)
        items = db.scalars(select(MediaItem).order_by(MediaItem.id)).all()
        for item in items:
            e.embed_media_item(db, settings, item)
        current = db.scalar(select(Embedding).where(Embedding.media_item_id == items[0].id))
        db.add(Embedding(media_item_id=items[0].id, embedding_model=settings.embedding_model,
                         embedding_source="obsolete", embedding_dimensions=2,
                         embedding_json=[1.0, 0.0], execution_provider="mock",
                         source_text_hash=current.source_text_hash))
        db.flush()
        expected = [item.id for item in items]
        assert list(search._semantic_scores_portable(db, settings, [1.0, 0.0], 2)) == expected
        assert [hit.media_item_id for hit in search.search_media(db, settings, "zzqueryzz", top_k=2)] == expected
        current.source_text_hash = None
        assert list(search._semantic_scores_portable(db, settings, [1.0, 0.0], 1)) == [items[1].id]


def test_manual_display_and_unknown_ai_embedding(settings, monkeypatch):
    with session_scope(settings) as db:
        _, item, row = establish(db, settings, monkeypatch)
        manual = Classification(media_item_id=item.id, model="manual", prompt_version="manual-v1",
                                summary="ownerword", raw_response_json={"provider": "manual"})
        db.add(manual)
        db.flush()
        row.summary = "neweraiword"
        row.created_at = manual.created_at
        row.compatibility_key = None
        assert _latest_classification({item.id: [row, manual]}, item.id) is manual
        assert item.id in search._lexical_scores(db, "ownerword")
        assert item.id not in search._lexical_scores(db, "neweraiword")
        item_id = item.id
    assert pipeline._classify_one(settings, item_id, pipeline.AiPacer(0), False).status == "withheld"
    assert pipeline._embed_one(settings, item_id, False).status == "embedded"
    assert pipeline._embed_one(settings, item_id, False).status == "skipped"


def test_pending_prefixes_and_message_limit(settings, fixture_dir, monkeypatch):
    from meme_arsenal.telegram_importer import import_fixture_messages
    from meme_arsenal.media import process_pending_media
    from meme_arsenal.models import MediaItem
    monkeypatch.setattr(ai, "extract_ocr_text", lambda *a: "")
    with session_scope(settings) as db:
        import_fixture_messages(db, settings, fixture_dir)
        process_pending_media(db, settings)
        ai.classify_pending_media(db, settings)
        items = db.scalars(select(MediaItem).order_by(MediaItem.id)).all()
        newest = items[-1]
        current = db.scalar(select(Classification).where(Classification.media_item_id == newest.id))
        current.compatibility_key = None
        db.add(Classification(media_item_id=newest.id, model="old", prompt_version="old"))
        older = items[0]
        duplicate = MediaItem(telegram_message_id=older.telegram_message_id, media_type="image",
                              original_path=older.original_path, processing_status="pending")
        db.add(duplicate)
        db.flush()
        assert pipeline.select_pipeline_media_ids(db, settings, limit=1) == [duplicate.id, older.id]
        assert pipeline.select_pipeline_media_ids(db, settings, media_ids=[]) == []
        assert pipeline.select_pipeline_media_ids(db, settings, media_ids=[newest.id]) == [newest.id]


@pytest.mark.parametrize("classification_status,paused,expected", [
    ("withheld", False, 1), ("failed", False, 0), ("withheld", True, 0),
])
def test_pipeline_authority_gate_and_counts(settings, monkeypatch, classification_status, paused, expected):
    monkeypatch.setattr(pipeline, "select_pipeline_media_ids", lambda *a, **kw: [1])
    monkeypatch.setattr(pipeline, "_process_one", lambda *a: pipeline.StageResult(1, True, "already_processed"))
    monkeypatch.setattr(pipeline, "_classify_one", lambda *a: pipeline.StageResult(1, False, classification_status))
    calls = []
    def embed(*a):
        calls.append(1)
        return pipeline.StageResult(1, True, "skipped")
    monkeypatch.setattr(pipeline, "_embed_one", embed)
    report = pipeline.run_media_pipeline(settings, ai_paused=paused, process_workers=1,
                                         classify_workers=1, embed_workers=1)
    assert len(calls) == expected
    assert report["embedded"] == report["embed_failed"] == 0
    assert report["classify_failed"] == (classification_status == "failed" and not paused)
