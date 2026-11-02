from __future__ import annotations

from meme_arsenal.config import Settings
from meme_arsenal.embeddings import embedding_vector_literal


def test_embedding_selection_preserves_request(monkeypatch):
    from types import SimpleNamespace
    import openai
    from meme_arsenal import embeddings
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(embedding=[0.25, 0.5])])
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: SimpleNamespace(
        embeddings=SimpleNamespace(create=create)))
    def google(settings, model, operation, payload):
        calls.append((model, operation, payload))
        return {"embedding": {"values": [0.25, 0.5]}}
    monkeypatch.setattr(embeddings, "post_google_model", google)
    for provider in ("mock", "openai", "google"):
        for override in (False, True):
            settings = Settings(ai_provider="mock" if override else provider,
                                embedding_provider=provider if override else None,
                                embedding_model="explicit-model", embedding_dimensions=2)
            assert len(embeddings.embed_text(settings, "hello")) == 2
            assert settings.ai_model == "gpt-5-mini"
    assert calls[0] == dict(model="explicit-model", input="hello", encoding_format="float", dimensions=2)
    assert calls[-1] == ("explicit-model", "embedContent", {
        "model": "models/explicit-model", "content": {"parts": [{"text": "hello"}]},
        "outputDimensionality": 2})


def test_missing_sdk_keys_do_not_fall_back(monkeypatch):
    import pytest
    import openai
    from meme_arsenal.ai import classify_with_openai
    from meme_arsenal.embeddings import embed_text
    from meme_arsenal.google_api import google_api_key
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)
    settings = Settings(classification_provider="openai", embedding_provider="openai",
                        remix_provider="google")
    with pytest.raises(openai.OpenAIError):
        embed_text(settings, "fixture")
    with pytest.raises(openai.OpenAIError):
        classify_with_openai(settings, {})
    with pytest.raises(RuntimeError) as error:
        google_api_key(settings)
    assert "AI_PROVIDER" not in str(error.value)


def test_embedding_vector_literal_keeps_sqlite_text_vector():
    settings = Settings(database_url="sqlite:///tmp/arsenal.sqlite")

    assert embedding_vector_literal(settings, [0.125, -0.5]) == "[0.12500000,-0.50000000]"


def test_embedding_vector_literal_formats_postgres_vector_value():
    settings = Settings(database_url="postgresql+psycopg://meme_arsenal:test@db/meme_arsenal")

    assert embedding_vector_literal(settings, [0.125, -0.5]) == "[0.12500000,-0.50000000]"
