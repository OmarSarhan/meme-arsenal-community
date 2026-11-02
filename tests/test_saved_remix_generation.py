from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import event

from meme_arsenal import ai, embeddings as e, saved_remix_generation as s
from meme_arsenal.db import session_scope
from meme_arsenal.models import Classification
from meme_arsenal.remix_execution import RemixExecutionCapture, RemixGenerationProvenance
from test_classification_inputs import seed
from test_embedding_execution import establish


def fallback(db, item):
    item.sha256 = "a" * 64
    row = Classification(media_item_id=item.id, model=s.MODEL, prompt_version=s.FORMAT,
                         summary="returned metadata", raw_response_json={"provider": s.PROVIDER})
    db.add(row)
    db.flush()
    capture = RemixExecutionCapture("remix-execution-v1", "new_job", "google",
                                    "models/" + "x" * 128, "1:1", "1K", False, 0, 0,
                                    "b" * 64, "historical-policy")
    provenance = RemixGenerationProvenance(capture, "c" * 64, "metadata-only", None,
                                         "model_json", 1)
    row.raw_response_json = {"provider": s.PROVIDER, s.PROVIDER:
                             s.build_saved_generation_evidence(item, row, provenance)}
    db.flush()
    return row, provenance


def test_contract_and_historical_resource(settings):
    with session_scope(settings) as db:
        _, item, _ = seed(db, settings)
        row, provenance = fallback(db, item)
        assert s.is_saved_generation_authoritative(item, row)
        assert len(row.raw_response_json[s.PROVIDER]["provenance"]["capture"]["model"]) > 128
        raw = deepcopy(row.raw_response_json)
        # Rebuild the historical authority declaration with the exact v1 field set.
        historical = deepcopy(raw[s.PROVIDER])
        assert "remix_style_guidance" not in historical["provenance"]["capture"]
        key = historical.pop("compatibility_key")
        assert key == s._digest({"domain": "saved-remix-generation-authority-v1",
                                 "model": s.MODEL, "prompt_version": s.FORMAT,
                                 "provider": s.PROVIDER, **historical})
        assert RemixGenerationProvenance.parse(historical["provenance"]).payload() == historical["provenance"]
        for invalid in (
            {s.PROVIDER: raw[s.PROVIDER]},
            {"provider": s.PROVIDER, s.PROVIDER: provenance.capture.payload()},
            {"provider": s.PROVIDER, s.PROVIDER: {**raw[s.PROVIDER], "provenance":
                replace(provenance, metadata_origin="local_fallback").payload()}},
        ):
            row.raw_response_json = invalid
            assert not s.is_saved_generation_authoritative(item, row)
            assert e.select_embedding_input(db, settings, item) is None
        row.raw_response_json = raw
        from meme_arsenal.models import MediaItem
        copied_item = MediaItem(id=item.id + 1, sha256=item.sha256)
        assert not s.is_saved_generation_authoritative(copied_item, row)
        with pytest.raises(ValueError):
            s.build_saved_generation_evidence(item, row, replace(provenance, metadata_origin="local_fallback"))
        for field in s._FIELDS:
            before = getattr(row, field)
            setattr(row, field, ["changed"] if isinstance(before, list) else "changed")
            assert not s.is_saved_generation_authoritative(item, row), field
            setattr(row, field, before)
        row.confidence = float("nan")
        assert not s.is_saved_generation_authoritative(item, row)


@pytest.mark.parametrize("field,value", [
    ("media_item_id", True), ("media_item_id", 999), ("output_sha256", "d" * 64),
    ("metadata_sha256", "d" * 64), ("compatibility_key", "d" * 64),
    ("format", "unknown"), ("provenance", None), ("provenance", {"capture": {}}),
    ("extra", "no"),
])
def test_envelope_tampering(settings, field, value):
    with session_scope(settings) as db:
        _, item, _ = seed(db, settings)
        row, _ = fallback(db, item)
        raw = deepcopy(row.raw_response_json)
        raw[s.PROVIDER][field] = value
        row.raw_response_json = raw
        assert not s.is_saved_generation_authoritative(item, row)
        assert e.select_embedding_input(db, settings, item) is None


@pytest.mark.parametrize("field,value", [
    ("model", "google"), ("prompt_version", "next"), ("media_item_id", 999),
    ("execution_provider", "google"), ("compatibility_key", "a" * 64),
    ("raw_response_json", {"provider": s.PROVIDER}),
    ("raw_response_json", {"provider": "remix"}), ("raw_response_json", []),
])
def test_row_tampering(settings, field, value):
    with session_scope(settings) as db:
        _, item, _ = seed(db, settings)
        row, _ = fallback(db, item)
        before = getattr(row, field)
        setattr(row, field, value)
        assert not s.is_saved_generation_authoritative(item, row)
        setattr(row, field, before)
        item.sha256 = None
        assert not s.is_saved_generation_authoritative(item, row)


def test_selector_execution_pending_and_purity(settings, monkeypatch):
    with session_scope(settings) as db:
        _, item, _ = seed(db, settings)
        row, _ = fallback(db, item)
        assert ai.classification_decision(db, settings, item) == ("unknown", None)
        calls = []
        monkeypatch.setattr(e, "embed_text", lambda cfg, text: calls.append(text) or [1] * cfg.embedding_dimensions)
        assert e.embed_pending_media(db, settings, limit=1)["embedded"] == 1
        embedded = e.embed_media_item(db, settings, item)
        assert len(calls) == 1
        assert embedded.source_text_hash == e.embedding_text_hash(e.embedding_source_text(db, item, row))
        assert embedded.execution_provider == settings.effective_embedding_provider
        assert embedded.embedding_dimensions == settings.embedding_dimensions
        changed = replace(settings, embedding_provider="google", embedding_model="new-model",
                          embedding_dimensions=3)
        refreshed = e.embed_media_item(db, changed, item)
        assert refreshed.execution_provider == "google"
        assert refreshed.embedding_model == "new-model"
        assert refreshed.embedding_dimensions == 3
        assert calls[-1] == calls[0]
        assert e.embed_media_item(db, changed, item) is refreshed
        assert len(calls) == 2
        def forbidden(*args, **kwargs):
            raise AssertionError("lookup performed work")
        monkeypatch.setattr(ai, "classify_media_item", forbidden)
        monkeypatch.setattr(ai, "extract_ocr_text", forbidden)
        monkeypatch.setattr(ai, "establish_derivative_hashes", forbidden)
        event.listen(db, "before_flush", forbidden)
        try:
            row.summary = "changed"
            assert e.select_embedding_input(db, settings, item) is None
            row.summary = "returned metadata"
            assert e.select_embedding_input(db, settings, item) is row
            item.sha256 = "f" * 64
            assert e.select_embedding_input(db, settings, item) is None
        finally:
            event.remove(db, "before_flush", forbidden)


def test_precedence_and_reserved_slot(settings, monkeypatch):
    with session_scope(settings) as db:
        _, item, normal = establish(db, settings, monkeypatch)
        row, _ = fallback(db, item)
        # Establish using the actual current input helper after changing original SHA.
        normal = ai.classify_media_item(db, settings, item, force=True)
        assert e.select_embedding_input(db, settings, item) is normal
        assert e.select_embedding_input(db, settings, item, row) is None
        manual = Classification(media_item_id=item.id, model="manual", prompt_version="manual-v1",
                                raw_response_json={"provider": "manual"})
        db.add(manual)
        db.flush()
        assert e.select_embedding_input(db, settings, item) is manual
        other = Classification(id=manual.id, media_item_id=item.id + 1)
        assert e.select_embedding_input(db, settings, item, other) is None
        reserved = replace(settings, ai_model=s.MODEL, classification_prompt_version=s.FORMAT)
        for force in (False, True):
            with pytest.raises(ValueError, match="reserved"):
                ai.classification_decision(db, reserved, item, force)
        with pytest.raises(ValueError, match="reserved"):
            e.select_embedding_input(db, reserved, item)


def test_reserved_slot_never_known(settings):
    with session_scope(settings) as db:
        _, item, _ = seed(db, settings)
        row, _ = fallback(db, item)
        row.execution_provider = "mock"
        row.compatibility_key = "a" * 64
        assert ai.classification_decision(db, settings, item) == ("unknown", None)
        assert e.select_embedding_input(db, settings, item) is None
