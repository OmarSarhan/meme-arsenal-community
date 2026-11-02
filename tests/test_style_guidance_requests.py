"""Synthetic request-boundary proof; all paid transports are explicitly replaced."""
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
import json
import sys

import pytest
from PIL import Image
from sqlalchemy import select

from meme_arsenal import ai, remix, embeddings
from meme_arsenal.db import session_scope
from meme_arsenal.google_api import GeminiApiError
from meme_arsenal.media import establish_derivative_hashes
from meme_arsenal.models import MediaItem
from meme_arsenal.remix_execution import capture_execution, ordered_text_digest
from meme_arsenal.style_guidance import render_style_guidance
from test_classification_inputs import seed
from test_remix import prepare_archive, gemini_image_response


def image_error():
    return GeminiApiError("Unable to process input image", status_code=400, status="INVALID_ARGUMENT")


@pytest.mark.parametrize("guidance", ["", '  terse\r\n日本語 "quoted"  '])
def test_classification_actual_boundaries(settings, tmp_path, monkeypatch, guidance):
    selected = replace(settings, classification_style_guidance=guidance)
    path = tmp_path / "synthetic.png"
    Image.new("RGB", (8, 8)).save(path)
    context = dict(caption="caption", media_type="image", mime_type="image/png", visual_paths=[path])
    defaults = json.loads((Path(__file__).parent / "fixtures/default_classification_requests.json").read_text())
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_text='{"summary":"observed"}')
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(
        OpenAI=lambda **kwargs: SimpleNamespace(responses=SimpleNamespace(create=create))))
    assert ai.classify_with_openai(selected, context).summary == "observed"
    assert calls[0]["text"]["format"]["schema"] == ai.CLASSIFICATION_JSON_SCHEMA
    assert calls[0]["input"][0]["content"][0]["text"] == defaults["openai"] + render_style_guidance(
        selected.classification_style_guidance)
    posts = []
    def post(config, model, method, payload):
        posts.append(payload)
        if len(posts) == 1:
            raise image_error()
        return {"candidates": [{"content": {"parts": [{"text": '{"summary":"observed"}'}]}}]}
    monkeypatch.setattr(ai, "post_google_model", post)
    payload, raw = ai.classify_with_google(selected, context)
    assert payload.summary == "observed" and raw["media_retry_without_inline_data"]
    base = defaults["google"] + render_style_guidance(selected.classification_style_guidance)
    assert posts[0]["contents"][0]["parts"][0]["text"] == base
    assert posts[1]["contents"][0]["parts"] == [{"text": base + ai.GOOGLE_MEDIA_RETRY_NOTICE}]
    assert all(p["generationConfig"]["responseSchema"] == ai.GEMINI_CLASSIFICATION_SCHEMA for p in posts)
    monkeypatch.setattr(ai, "post_google_model", lambda *a: {"candidates": [{"content": {"parts": [{"text": "bad"}]}}]})
    with pytest.raises(ValueError, match="not JSON"):
        ai.classify_with_google(selected, context)
    monkeypatch.setattr(ai, "post_google_model", lambda *a: {"promptFeedback": {"blockReason": "SAFETY"}})
    assert ai.classify_with_google(selected, context)[0].safety_flags


def test_guidance_identity_refresh_and_embedding(settings, monkeypatch):
    monkeypatch.setattr(ai, "extract_ocr_text", lambda _: "")
    with session_scope(settings) as db:
        _, item, path = seed(db, settings)
        original_bytes = path.read_bytes()
        row = ai.classify_media_item(db, settings, item)
        vector = embeddings.embed_media_item(db, settings, item)
        before = row.summary, row.compatibility_key, row.raw_response_json
        changed = replace(settings, classification_style_guidance="terse", remix_style_guidance="comic")
        with monkeypatch.context() as guard:
            guard.setattr(ai, "media_context", lambda *a: pytest.fail("lookup prepared media"))
            assert ai.classification_decision(db, changed, item)[0] == "execute"
        def failed(*a):
            raise RuntimeError("synthetic failure")
        monkeypatch.setattr(ai, "classify_with_mock", failed)
        with pytest.raises(RuntimeError):
            ai.classify_media_item(db, changed, item)
        assert (row.summary, row.compatibility_key, row.raw_response_json) == before
        monkeypatch.undo()
        monkeypatch.setattr(ai, "extract_ocr_text", lambda _: "")
        assert ai.classify_media_item(db, changed, item).summary == before[0]
        assert embeddings.embed_media_item(db, changed, item) is vector
        remix_only = replace(changed, remix_style_guidance="other")
        assert embeddings.embed_media_item(db, remix_only, item) is vector
        old_digest = vector.source_text_hash
        monkeypatch.setattr(ai, "classify_with_mock", lambda _: ai.ClassificationPayload(summary="changed actual text"))
        ai.classify_media_item(db, changed, item, force=True)
        embeddings.embed_media_item(db, changed, item)
        assert vector.source_text_hash != old_digest
        assert path.read_bytes() == original_bytes


@pytest.mark.parametrize("retained", ["", "retained A"])
def test_remix_retained_initial_and_internal_retry(settings, fixture_dir, monkeypatch, retained):
    prepare_archive(settings, fixture_dir)
    original = replace(settings, remix_provider="google", google_api_key="synthetic", remix_style_guidance=retained)
    capture = capture_execution(original, "new_direct")
    current = replace(original, remix_style_guidance="current B")
    calls = []
    def post(config, model, method, payload):
        assert config.remix_style_guidance == retained
        calls.append(payload)
        if len(calls) == 1:
            raise image_error()
        return gemini_image_response()
    monkeypatch.setattr(remix, "post_google_model", post)
    with session_scope(current) as db:
        ids = list(db.scalars(select(MediaItem.id).order_by(MediaItem.id)))
        result = remix.generate_remix(db, current, *ids, execution_capture=capture, attempt_number=2)
    for call in calls:
        text = call["contents"][0]["parts"][0]["text"]
        assert "current B" not in text
        assert (render_style_guidance(retained) in text) if retained else "Operator style guidance" not in text
    evidence = result.generation_provenance
    assert evidence.capture == capture and evidence.attempt_number == 2
    assert evidence.attachment_mode == "metadata-only" and evidence.metadata_origin == "model_json"
    assert evidence.submitted_text_digest == ordered_text_digest([
        p["text"] for p in calls[-1]["contents"][0]["parts"] if "text" in p])


@pytest.mark.parametrize("provider", ["mock", "openai", "google"])
def test_default_key_fixture(settings, provider):
    selected = replace(settings, classification_provider=provider)
    with session_scope(settings) as db:
        _, item, _ = seed(db, settings)
        establish_derivative_hashes(settings, item)
        key = ai.classification_compatibility_key(db, selected, item)
        assert key == DEFAULT_KEYS[provider]
        assert key == ai.classification_compatibility_key(db, replace(selected, classification_style_guidance=" \r\n"), item)
        assert key != ai.classification_compatibility_key(db, replace(selected, classification_style_guidance="terse"), item)


DEFAULT_KEYS = {
    "mock": "dc0bcb4d180c205f815ac74b5252147ae6fceba91035f65bf50a3afd8a1d127c",
    "openai": "26cc29b30a79e895edd7d9fbcfe6f838440fb797473e5be2b83ed712b44377ee",
    "google": "59b7cd922672e02ff3196a2412bfb236e2f6485339a4b2a7eaeefc29bc79b98a",
}
