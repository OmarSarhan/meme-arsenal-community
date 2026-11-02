from __future__ import annotations

import html
import json
import shutil
import logging
from dataclasses import replace
from datetime import datetime, timezone
import subprocess
import uuid
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from meme_arsenal.ai import classify_pending_media
from meme_arsenal.db import session_scope
from meme_arsenal.media import process_pending_media, repair_missing_media_references, reserve_absolute
from meme_arsenal.models import (
    AnonymousSession,
    Classification,
    MediaItem,
    PublicMediaShare,
    RemixJobRecord,
    RemixSourceRecord,
    TelegramMessage,
)
from meme_arsenal.online import _public_media_filter_statement, create_app, refresh_public_indexes
from meme_arsenal.telegram_importer import import_fixture_messages


def _online_settings(settings):
    return replace(
        settings,
        public_session_secret="test-secret-for-signed-cookie",
        public_session_cookie_secure=False,
        public_sharing_enabled=True,
        redis_url="redis://localhost:1/0",
    )


def test_online_app_rejects_insecure_production_settings(settings):
    production = replace(
        settings,
        app_env="production",
        public_site_url="https://example.test",
        public_session_cookie_secure=True,
    )
    with pytest.raises(RuntimeError, match="PUBLIC_SESSION_SECRET"):
        create_app(production)

    production = replace(production, public_session_secret="s" * 32)
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        create_app(production)


def test_online_health_reports_dependency_failures(settings):
    settings = _online_settings(settings)
    response = TestClient(create_app(settings)).get("/api/health")

    assert response.status_code == 503
    assert response.json()["database_ok"] is True
    assert response.json()["redis_ok"] is False


def test_online_postgres_search_uses_public_search_document_fts_index():
    statement = _public_media_filter_statement(
        dialect_name="postgresql",
        session_id="test-session",
        q="merk tax",
        media_type="all",
        topic="all",
        tone="all",
        reaction_type="all",
        meme_format="all",
        language="all",
        favorite=False,
        archived=False,
    )

    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "to_tsvector('simple', public_search_documents.document_text) @@ to_tsquery('simple'" in sql
    assert compiled.params["to_tsquery_1"] == "merk:* & tax:*"
    assert "telegram_messages" not in sql


def test_online_api_lists_public_items_and_session_curation(settings, fixture_dir):
    settings = _online_settings(settings)
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)
        process_pending_media(session, settings)
        classify_pending_media(session, settings)
        refresh_public_indexes(session, settings=settings)

    client = TestClient(create_app(settings))

    response = client.get("/api/items")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["items"]) == 2
    assert payload["total"] == 2
    limited = client.get("/api/items", params={"limit": 1}).json()
    assert len(limited["items"]) == 1
    assert limited["total"] == 2
    assert limited["next_cursor"] is not None
    deploy_search = client.get("/api/items", params={"limit": 1, "q": "deploy failed"}).json()
    assert deploy_search["total"] == 1
    assert len(deploy_search["items"]) == 1
    assert "deploy" in deploy_search["items"][0]["caption"].lower()
    state = client.get("/api/catalog-state")
    assert state.status_code == 200
    assert state.json()["item_count"] == 2
    first = payload["items"][0]
    assert first["original_path"].startswith("/media/originals/")
    head = client.head(first["original_path"])
    assert head.status_code == 200
    assert int(head.headers["content-length"]) > 0
    assert first["favorite"] is False

    patch = client.patch(
        f"/api/session/items/{first['id']}",
        json={"favorite": True, "archived": True, "manual_tags": ["keeper"], "notes": "private note"},
    )
    assert patch.status_code == 200
    updated = patch.json()
    assert updated["favorite"] is True
    assert updated["archived"] is True
    assert updated["manual_tags"] == ["keeper"]
    assert updated["notes"] == "private note"

    detail = client.get(f"/api/items/{first['id']}").json()
    assert detail["favorite"] is True
    assert detail["manual_tags"] == ["keeper"]

    visible = client.get("/api/items").json()
    assert visible["total"] == 1
    assert first["id"] not in {item["id"] for item in visible["items"]}
    inclusive = client.get("/api/items?include_hidden=true").json()
    assert inclusive["total"] == 2
    assert {item["id"] for item in inclusive["items"]} == {item["id"] for item in payload["items"]}
    assert sum(item["archived"] for item in inclusive["items"]) == 1
    assert client.get("/api/items?favorite=true").json()["total"] == 0
    assert client.get("/api/items?favorite=true&include_hidden=true").json()["total"] == 1
    assert client.get("/api/items?archived=true").json()["total"] == 1
    for params in ({}, {"include_hidden": True}, {"archived": True}):
        assert client.get("/api/items/random", params=params).json()["id"] != first["id"]
    other = TestClient(create_app(settings)).get("/api/items").json()
    assert other["total"] == 1
    assert first["id"] not in {item["id"] for item in other["items"]}


def test_online_api_random_item_honors_search_filters(settings, fixture_dir):
    settings = _online_settings(settings)
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)
        process_pending_media(session, settings)
        classify_pending_media(session, settings)
        refresh_public_indexes(session, settings=settings)

    client = TestClient(create_app(settings))
    listed = client.get("/api/items").json()["items"]
    listed_ids = {item["id"] for item in listed}

    random_any = client.get("/api/items/random")
    assert random_any.status_code == 200
    assert random_any.json()["id"] in listed_ids

    random_smug = client.get("/api/items/random", params={"q": "smug"})
    assert random_smug.status_code == 200
    assert "smug" in random_smug.json()["caption"].lower()

    missing = client.get("/api/items/random", params={"q": "not in this arsenal"})
    assert missing.status_code == 404


def test_online_public_share_link_lifecycle(settings, fixture_dir):
    settings = _online_settings(settings)
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)
        process_pending_media(session, settings)
        classify_pending_media(session, settings)
        refresh_public_indexes(session, settings=settings)
        media_item = session.scalars(
            select(MediaItem).where(MediaItem.media_type == "image").order_by(MediaItem.id)
        ).first()
        assert media_item is not None
        message = session.get(TelegramMessage, media_item.telegram_message_id)
        assert message is not None
        message.caption = "Shared <script>alert('nope')</script> caption"
        original_summary = media_item.classifications[0].summary
        assert original_summary
        media_item_id = media_item.id

    app = create_app(settings)
    owner = TestClient(app)
    created = owner.post(f"/api/items/{media_item_id}/share")

    assert created.status_code == 200
    share = created.json()
    assert share["active"] is True
    assert share["created_at"]
    assert share["url"].startswith(f"{settings.public_site_url}/share/")
    share_path = urlsplit(share["url"]).path
    token = share_path.removeprefix("/share/")
    assert len(token) >= 64
    assert (
        owner.post(
            f"/api/items/{media_item_id}/share",
            headers={"Origin": "https://attacker.example"},
        ).status_code
        == 403
    )

    assert owner.get(f"/api/items/{media_item_id}/share").json() == share
    assert owner.post(f"/api/items/{media_item_id}/share").json()["url"] == share["url"]

    public = TestClient(app)
    page = public.get(share_path)
    assert page.status_code == 200
    assert 'id="share"' in page.text
    assert "navigator.share" in page.text
    assert "viewport-fit=cover" in page.text
    # Caption edits do not override the description shown throughout the dashboard.
    assert f"<p>{html.escape(original_summary)}</p>" in page.text
    assert "Shared &lt;script&gt;" not in page.text

    # The dashboard's Description editor writes summary through this endpoint.
    edited_summary = "Edited <script>alert('nope')</script> & \"summary\""
    edited = owner.patch(
        f"/api/items/{media_item_id}/metadata", json={"summary": edited_summary},
    )
    assert edited.status_code == 200
    assert edited.json()["summary"] == edited_summary
    page = public.get(share_path)
    assert page.status_code == 200
    escaped_summary = "Edited &lt;script&gt;alert(&#x27;nope&#x27;)&lt;/script&gt; &amp; &quot;summary&quot;"
    assert f"<p>{escaped_summary}</p>" in page.text
    assert f'<meta name="description" content="{escaped_summary}">' in page.text
    assert f'<meta property="og:description" content="{escaped_summary}">' in page.text
    assert "<script>alert('nope')</script>" not in page.text
    assert page.headers["cache-control"] == "private, no-store"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-robots-tag"] == "noindex, nofollow, noarchive"

    cleared = owner.patch(f"/api/items/{media_item_id}/metadata", json={"summary": ""})
    assert cleared.status_code == 200
    page = public.get(share_path)
    assert page.status_code == 200
    assert "<p>Shared &lt;script&gt;alert(&#x27;nope&#x27;)&lt;/script&gt; caption</p>" in page.text
    assert "<script>alert('nope')</script>" not in page.text

    shared_media = public.get(f"{share_path}/media")
    assert shared_media.status_code == 200
    assert shared_media.content
    assert shared_media.headers["cache-control"] == "private, no-store"
    assert public.head(f"{share_path}/poster").status_code == 200
    assert public.get(f"{share_path}x").status_code == 404

    recalled = owner.delete(f"/api/items/{media_item_id}/share")
    assert recalled.status_code == 200
    assert recalled.json()["active"] is False
    assert recalled.json()["url"] is None
    assert recalled.json()["revoked_at"]
    assert public.get(share_path).status_code == 404
    assert public.get(f"{share_path}/media").status_code == 404

    replacement = owner.post(f"/api/items/{media_item_id}/share").json()
    replacement_path = urlsplit(replacement["url"]).path
    assert replacement["active"] is True
    assert replacement["url"] != share["url"]
    assert public.get(share_path).status_code == 404
    assert public.get(replacement_path).status_code == 200

    with session_scope(settings) as session:
        shares = session.scalars(
            select(PublicMediaShare).where(PublicMediaShare.media_item_id == media_item_id)
        ).all()
        assert len(shares) == 1
        assert shares[0].active is True


@pytest.mark.parametrize(
    ("origin", "summary", "caption", "expected"),
    [
        pytest.param(
            "telegram", '  <b>Summary</b> & "quoted"  ', "Caption",
            '<b>Summary</b> & "quoted"', id="summary-first",
        ),
        pytest.param(
            "telegram", "", '  <b>Caption</b> & "quoted"  ',
            '<b>Caption</b> & "quoted"', id="empty-summary",
        ),
        pytest.param("telegram", " \n ", "Caption", "Caption", id="blank-summary"),
        pytest.param(
            "telegram", None, "<b>Caption</b>", "<b>Caption</b>", id="missing-classification",
        ),
        pytest.param("telegram", None, None, "No description available.", id="missing-fields"),
        pytest.param("telegram", "", " \n ", "No description available.", id="blank-fields"),
        pytest.param(
            "remix", "", "<b>Internal caption</b>", "No description available.",
            id="remix-no-caption-fallback",
        ),
    ],
)
def test_online_public_share_description_precedence(
    settings, fixture_dir, origin, summary, caption, expected,
):
    settings = _online_settings(settings)
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)
        process_pending_media(session, settings)
        media_item = session.scalars(select(MediaItem).order_by(MediaItem.id)).first()
        assert media_item is not None
        media_item.origin = origin
        media_item.telegram_message.caption = caption
        # None represents an item that has not been classified yet.
        if summary is not None:
            session.add(Classification(
                media_item_id=media_item.id, model="manual", prompt_version="manual-v1",
                summary=summary,
            ))
        media_item_id = media_item.id

    client = TestClient(create_app(settings))
    created = client.post(f"/api/items/{media_item_id}/share")
    assert created.status_code == 200
    page = client.get(urlsplit(created.json()["url"]).path)
    assert page.status_code == 200
    escaped = html.escape(expected)
    assert f"<p>{escaped}</p>" in page.text
    assert f'<meta name="description" content="{escaped}">' in page.text
    assert f'<meta property="og:description" content="{escaped}">' in page.text
    assert f'alt="{escaped}"' in page.text
    assert "<b>" not in page.text


def test_online_api_hides_public_items_with_missing_originals(settings, fixture_dir):
    settings = _online_settings(settings)
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)
        process_pending_media(session, settings)
        classify_pending_media(session, settings)
        items = session.scalars(select(MediaItem).order_by(MediaItem.id.desc())).all()
        missing_item = items[0]
        fallback_item = items[1]
        missing_original = reserve_absolute(settings, missing_item.original_path)
        missing_thumbnail = reserve_absolute(settings, fallback_item.thumbnail_path)
        assert missing_original is not None
        assert missing_thumbnail is not None
        missing_original.unlink()
        missing_thumbnail.unlink()
        refresh_public_indexes(session, settings=settings)
        missing_id = missing_item.id
        fallback_id = fallback_item.id

    client = TestClient(create_app(settings))

    response = client.get("/api/items", params={"limit": 1})
    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload["items"]] == [fallback_id]

    assert client.get(f"/api/items/{missing_id}").status_code == 404

    detail = client.get(f"/api/items/{fallback_id}").json()
    assert detail["thumbnail_path"] is None
    assert detail["preview_path"].startswith("/media/previews/")

    tags = client.get("/api/tags").json()
    assert tags["media_type"] == [{"value": "image", "count": 1}]


def test_repair_missing_media_references_demotes_missing_originals(settings, fixture_dir):
    settings = _online_settings(settings)
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)
        process_pending_media(session, settings)
        media_item = session.scalars(select(MediaItem).order_by(MediaItem.id.desc())).first()
        original = reserve_absolute(settings, media_item.original_path)
        assert original is not None
        original.unlink()

        counts = repair_missing_media_references(session, settings)

        assert counts["missing_originals"] == 1
        assert media_item.processing_status == "failed"
        assert media_item.public_status == "draft"
        assert media_item.visibility == "private"
        assert media_item.thumbnail_path is None
        assert media_item.preview_path is None
        assert media_item.hover_preview_path is None


def test_online_api_blocks_private_media_paths(settings):
    client = TestClient(create_app(_online_settings(settings)))

    assert client.get("/media/originals/missing.png").status_code == 404
    assert client.get("/media/../data/telegram.session").status_code == 404
    assert client.get("/media/private.txt").status_code == 404


def test_online_media_auth_session_closes_before_file_stream(settings, monkeypatch):
    settings = _online_settings(settings)
    media = settings.previews_dir / "public-preview.txt"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"preview")

    app = create_app(settings)
    events: list[str] = []

    class DummySession:
        def __enter__(self):
            events.append("open")
            return self

        def __exit__(self, exc_type, exc, traceback):
            events.append("close")

    def allow_media_path(db, checked_settings, path, *, session_id):
        events.append(f"check:{path}")
        return True

    app.state.runtime.session_local = lambda: DummySession()
    monkeypatch.setattr("meme_arsenal.online._media_path_allowed", allow_media_path)

    client = TestClient(app)
    response = client.get("/media/previews/public-preview.txt")

    assert response.status_code == 200
    assert response.content == b"preview"
    assert events == ["open", "check:media/previews/public-preview.txt", "close"]


def test_online_app_serves_spa_without_catalog_fallback(settings):
    settings = _online_settings(settings)
    settings.dashboard_dist_dir.mkdir(parents=True, exist_ok=True)
    (settings.dashboard_dist_dir / "index.html").write_text("<!doctype html><div id=\"root\"></div>", encoding="utf-8")
    asset_dir = settings.dashboard_dist_dir / "assets"
    asset_dir.mkdir()
    (asset_dir / "app.js").write_text("window.__arsenal = true;", encoding="utf-8")

    client = TestClient(create_app(settings))

    assert client.get("/").status_code == 200
    assert client.get("/deep/dashboard/route").status_code == 200
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/catalog/index.json").status_code == 404
    assert client.get("/api/does-not-exist").status_code == 404


def test_online_api_upload_to_remix_and_save_draft(settings, fixture_dir):
    settings = _online_settings(settings)
    client = TestClient(create_app(settings))

    upload_path = fixture_dir / "deploy-fails.png"
    upload = client.post(
        "/api/remix/uploads",
        files={"file": ("deploy-fails.png", upload_path.read_bytes(), "image/png")},
    )
    assert upload.status_code == 200
    draft = upload.json()
    assert draft["origin"] == "dashboard_upload"
    assert draft["public_status"] == "draft"
    assert draft["visibility"] == "private"
    assert draft["processing_status"] == "processed"
    assert draft["original_path"].startswith("/media/originals/")
    assert client.get(draft["original_path"]).status_code == 200
    another_client = TestClient(create_app(settings))
    assert another_client.get(draft["original_path"]).status_code == 200

    pre_save_items = client.get("/api/items").json()["items"]
    assert all(item["id"] != draft["id"] for item in pre_save_items)

    # An old draft should enter the gallery at its save time, not its upload time.
    with session_scope(settings) as session:
        draft_item = session.get(MediaItem, draft["id"])
        draft_item.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        draft_item.telegram_message.date = datetime(2020, 1, 1, tzinfo=timezone.utc)

    save = client.post(f"/api/remix/uploads/{draft['id']}/save")
    assert save.status_code == 200
    saved = save.json()
    assert saved["status"] == "saved"
    assert saved["item"]["id"] == draft["id"]
    assert saved["item"]["public_status"] == "published"
    assert saved["item"]["visibility"] == "public"
    assert datetime.fromisoformat(saved["item"]["saved_at"]) > datetime(2020, 1, 1, tzinfo=timezone.utc)
    repeated_save = client.post(f"/api/remix/uploads/{draft['id']}/save").json()
    assert repeated_save["item"]["saved_at"] == saved["item"]["saved_at"]

    post_save_items = client.get("/api/items").json()["items"]
    assert any(item["id"] == draft["id"] for item in post_save_items)
    assert post_save_items[0]["saved_at"] == saved["item"]["saved_at"]

    assert another_client.get(draft["original_path"]).status_code == 200
    assert another_client.post(f"/api/remix/uploads/{draft['id']}/save").status_code == 200


def test_online_remix_recovery_is_session_scoped_and_includes_private_sources(settings, fixture_dir):
    settings = _online_settings(settings)
    app = create_app(settings)
    owner = TestClient(app)
    empty = owner.get("/api/remix/jobs")
    assert empty.json() == {"jobs": [], "next_cursor": None}
    assert "set-cookie" not in empty.headers
    drafts = []
    for name in ("left.png", "right.png"):
        response = owner.post("/api/remix/uploads", files={
            "file": (name, (fixture_dir / "deploy-fails.png").read_bytes(), "image/png"),
        })
        assert response.status_code == 200
        drafts.append(response.json()["id"])
    with session_scope(settings) as db:
        session_id = db.scalar(select(AnonymousSession.id))
        db.add(RemixJobRecord(id="recover-me", session_id=session_id, left_id=drafts[0], right_id=drafts[1], status="ready"))

    assert [job["id"] for job in owner.get("/api/remix/jobs").json()["jobs"]] == ["recover-me"]
    assert "source_items" not in owner.get("/api/remix/jobs/recover-me").json()
    recovered = owner.get("/api/remix/jobs/recover-me?include_sources=true").json()
    assert [item["id"] for item in recovered["source_items"]] == drafts
    assert all(item["visibility"] == "private" for item in recovered["source_items"])
    stranger = TestClient(app)
    assert stranger.get("/api/remix/jobs").json()["jobs"][0]["id"] == "recover-me"
    assert stranger.get("/api/remix/jobs/recover-me?include_sources=true").status_code == 200
    assert owner.get("/api/remix/jobs?limit=1000").status_code == 422


def test_online_playback_diagnostics_are_bounded_and_respect_access(settings, fixture_dir, caplog, monkeypatch):
    monkeypatch.setattr(logging.getLogger("uvicorn.error"), "disabled", False)
    settings = _online_settings(settings)
    with session_scope(settings) as db:
        import_fixture_messages(db, settings, fixture_dir)
        process_pending_media(db, settings)
        classify_pending_media(db, settings)
        refresh_public_indexes(db, settings=settings)
        item = db.scalar(select(MediaItem).order_by(MediaItem.id))
        item.media_type = "video"
        item_id = item.id
    app = create_app(settings)
    client = TestClient(app)
    endpoint = f"/api/items/{item_id}/playback-events"
    payload = dict(attempt="test-play", event="stalled", surface="gallery", source="playback", elapsed_ms=4000,
                   current_time=0, ready_state=1, network_state=2, buffered_end=0, paused=False, muted=False,
                   error_code=None, h264_aac="probably")
    with caplog.at_level("INFO", logger="uvicorn.error"):
        assert client.post(endpoint, json=payload, headers={"User-Agent": "EdgA/test"}).status_code == 204
    diagnostic = next(record.message for record in caplog.records if "playback_diagnostic" in record.message)
    assert '"event": "stalled"' in diagnostic
    assert "EdgA/test" in diagnostic
    assert client.post(endpoint, json={**payload, "url": "private-media"}).status_code == 422
    assert client.post(endpoint, json={**payload, "ready_state": 8}).status_code == 422
    assert client.post(endpoint, json=payload, headers={"Origin": "https://attacker.example"}).status_code == 403
    share = client.post(f"/api/items/{item_id}/share").json()
    share_path = urlsplit(share["url"]).path
    public = TestClient(app)
    assert public.post(share_path + "/events", json={**payload, "surface": "share", "source": "share"}).status_code == 204
    assert share_path not in diagnostic
    client.delete(f"/api/items/{item_id}/share")
    assert public.post(share_path + "/events", json=payload).status_code == 404
    with session_scope(settings) as db:
        db.get(MediaItem, item_id).visibility = "private"
    assert public.post(endpoint, json=payload).status_code == 404


def test_online_api_limits_upload_size_and_rejects_unknown_types(settings):
    settings = replace(_online_settings(settings), public_upload_max_mb=0.000001)
    client = TestClient(create_app(settings))

    too_large = client.post(
        "/api/remix/uploads",
        files={"file": ("large.png", b"not-really-an-image", "image/png")},
    )
    assert too_large.status_code == 413
    assert list(settings.remix_drafts_dir.iterdir()) == []

    unsupported = client.post(
        "/api/remix/uploads",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert unsupported.status_code == 415


def test_online_api_cleans_files_when_upload_classification_fails(monkeypatch, settings, fixture_dir):
    settings = _online_settings(settings)
    client = TestClient(create_app(settings))
    monkeypatch.setattr(
        "meme_arsenal.online.classify_media_item",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("classification unavailable")),
    )

    response = client.post(
        "/api/remix/uploads",
        files={"file": ("failed.png", (fixture_dir / "deploy-fails.png").read_bytes(), "image/png")},
    )

    assert response.status_code == 400
    assert not any(settings.originals_dir.rglob("*"))
    assert not any(settings.thumbnails_dir.rglob("*"))
    assert not any(settings.previews_dir.rglob("*"))
    assert list(settings.remix_drafts_dir.iterdir()) == []


def test_online_api_rate_limits_paid_remix_analysis(monkeypatch, settings, fixture_dir):
    settings = replace(_online_settings(settings), public_remix_analyses_per_hour=1)
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)
        process_pending_media(session, settings)
        classify_pending_media(session, settings)
        refresh_public_indexes(session, settings=settings)
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()

    calls: list[tuple[int, int]] = []

    class FakeAnalysis:
        def payload(self):
            return {"source": "test", "controls": []}

    def fake_analysis(_db, _settings, left_id, right_id):
        calls.append((left_id, right_id))
        return FakeAnalysis()

    monkeypatch.setattr("meme_arsenal.online.analyze_remix_tweaks", fake_analysis)
    settings = replace(settings, ai_provider="google", google_api_key="fake-analysis-key")
    client = TestClient(create_app(settings))
    payload = {"left_id": media_ids[0], "right_id": media_ids[1]}

    assert client.post("/api/remix/analysis", json=payload).status_code == 200
    limited = client.post("/api/remix/analysis", json=payload)
    assert limited.status_code == 429
    assert limited.json()["detail"]["error"] == "rate_limit_exceeded"
    assert calls == [(media_ids[0], media_ids[1])]


@pytest.mark.parametrize("provider,key", [
    ("mock", "credential-sentinel"), ("openai", "credential-sentinel"),
    ("google", None), ("google", ""), ("google", "   "),
    ("GOOGLE", "fake-configured-key"),
])
@pytest.mark.parametrize("override", [False, True])
def test_remix_configuration_readiness(monkeypatch, settings, fixture_dir, provider, key, override):
    from sqlalchemy import func
    from meme_arsenal.remix import (
        RemixUnavailableError, analyze_remix_tweaks, generate_remix, remix_available,
        remix_unavailable_reason,
    )
    from meme_arsenal.remix_server import RemixJobStore

    settings = _online_settings(settings)
    with session_scope(settings) as db:
        import_fixture_messages(db, settings, fixture_dir)
        process_pending_media(db, settings)
        classify_pending_media(db, settings)
        refresh_public_indexes(db, settings=settings)
        ids = db.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
    settings = replace(settings, google_api_key=key, **(
        {"ai_provider": "openai", "remix_provider": provider} if override
        else {"ai_provider": provider}
    ))
    configured = provider.lower() == "google" and bool(key and key.strip())
    reason = remix_unavailable_reason(settings)
    assert remix_available(settings) is configured
    assert (reason is None) is configured
    if reason:
        assert "AI_PROVIDER" in reason or "GEMINI_API_KEY" in reason
        assert "credential-sentinel" not in reason

    def forbidden(*args, **kwargs):
        pytest.fail("Unavailable Remix must not submit or invoke a provider")

    enqueued = []
    monkeypatch.setattr("meme_arsenal.online.enqueue_remix_job", forbidden)
    monkeypatch.setattr("meme_arsenal.online.analyze_remix_tweaks", forbidden)
    store = RemixJobStore(settings)
    client = TestClient(create_app(settings))
    try:
        assert client.get("/api/remix/health").json() == store.health()
        assert store.health()["enabled"] is configured
        assert store.health()["provider"] == provider.lower()
        assert store.health()["message"] == (reason or "Ready")
        monkeypatch.setattr(store._executor, "submit", forbidden)
        if not configured:
            with pytest.raises(RemixUnavailableError) as error:
                store.create_job(*ids)
            assert str(error.value) == reason
            assert store._jobs == {}
            with session_scope(settings) as db:
                for operation in (analyze_remix_tweaks, generate_remix):
                    with pytest.raises(RemixUnavailableError) as error:
                        operation(db, settings, *ids)
                    assert str(error.value) == reason
        else:
            class Analysis:
                def payload(self):
                    return {"source": "stub", "controls": []}
            monkeypatch.setattr("meme_arsenal.online.analyze_remix_tweaks", lambda *a: Analysis())
            monkeypatch.setattr("meme_arsenal.online.enqueue_remix_job", lambda s, job: enqueued.append(job))
            monkeypatch.setattr(store._executor, "submit", lambda *a: None)
            assert store.create_job(*ids).status == "queued"
        payload = {"left_id": ids[0], "right_id": ids[1]}
        for route in ("analysis", "jobs"):
            response = client.post(f"/api/remix/{route}", json=payload)
            if configured:
                assert response.status_code == (200 if route == "analysis" else 202)
            else:
                assert response.status_code == 503
                assert response.json() == {"detail": reason}
        with session_scope(settings) as db:
            assert db.scalar(select(func.count()).select_from(RemixJobRecord)) == int(configured)
        assert len(enqueued) == int(configured)
    finally:
        store.shutdown()


@pytest.mark.parametrize("metadata_change", ["refresh", "manual"])
def test_online_api_ready_remix_job_saves_only_when_requested(settings, fixture_dir, monkeypatch, metadata_change):
    settings = _online_settings(settings)
    from meme_arsenal.remix_execution import capture_execution, RemixGenerationProvenance
    capture = capture_execution(replace(settings, remix_provider="google",
                                        remix_style_guidance="PRIVATE_ROUTE_GUIDANCE_39"), "new_job")
    provenance = RemixGenerationProvenance(capture, "a" * 64, "metadata-only", None, "model_json", 1)
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)
        process_pending_media(session, settings)
        classify_pending_media(session, settings)
        refresh_public_indexes(session, settings=settings)
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        # Keep the fixture poster, but exercise video routing with a mocked playback cache.
        session.get(MediaItem, media_ids[0]).media_type = "video"

    playback_calls = []

    def fake_playback(_settings, item_id, path):
        playback_calls.append(item_id)
        return path

    monkeypatch.setattr("meme_arsenal.online.ensure_browser_playback", fake_playback)

    client = TestClient(create_app(settings))
    assert client.get("/api/items").status_code == 200

    with session_scope(settings) as session:
        session_id = session.scalars(select(AnonymousSession.id)).first()
        assert session_id is not None
        job_id = uuid.uuid4().hex
        settings.remix_drafts_dir.mkdir(parents=True, exist_ok=True)
        draft_path = settings.remix_drafts_dir / f"{job_id}.png"
        Image.new("RGB", (96, 96), color=(90, 120, 180)).save(draft_path, "PNG")
        session.add(
            RemixJobRecord(
                id=job_id,
                session_id=session_id,
                left_id=media_ids[0],
                right_id=media_ids[1],
                safety_mode="standard",
                status="ready",
                message="Ready",
                execution_capture_json=capture.payload(),
                tweaks_json={
                    "analysis_prompt_version": "test-v1", "summary": "Saved settings",
                    "controls": [{"id": "chaos", "label": "Chaos", "description": "Amount of chaos",
                                  "low_label": "Calm", "high_label": "Wild", "default_value": 50}],
                    "values": {"chaos": 68}, "schizo_level": 2, "chronically_online_level": 1,
                    "no_text": True, "comic_strip": False, "anti_slop": True,
                },
                result_json={
                    "result": {
                        "summary": "Ready generated remix",
                        "generation_provenance": provenance.payload(),
                        "visible_text": "SAVE ONLY",
                        "narration": "A generated remix waits for explicit save.",
                        "language": "en",
                        "meme_format": "generated remix meme",
                        "tone": ["playful"],
                        "reaction_type": ["surprise"],
                        "topics": ["worker", "remix"],
                        "keywords": ["generated", "remix"],
                        "people_or_characters": [],
                        "safety_flags": [],
                        "search_phrases": ["ready generated remix"],
                        "confidence": 0.9,
                        "image_path": draft_path.name,
                        "safety_mode": "standard",
                    }
                },
            )
        )

    before_save = client.get("/api/items").json()["items"]
    assert all(item["summary"] != "Ready generated remix" for item in before_save)
    assert client.get(f"/api/remix/jobs/{job_id}/image").status_code == 200
    from io import BytesIO

    draft_card = client.get(f"/api/remix/jobs/{job_id}/card")
    assert draft_card.status_code == 200
    assert Image.open(BytesIO(draft_card.content)).size == (1000, 1460)
    assert "no-store" in draft_card.headers["cache-control"]
    draft_page = client.get(f"/api/remix/jobs/{job_id}/card?page=true")
    assert draft_page.status_code == 200
    assert "Ready generated remix" in draft_page.text
    assert "Amount of chaos" in draft_page.text
    assert "Saved settings" in draft_page.text
    assert "PRIVATE_ROUTE_GUIDANCE_39" not in draft_page.text
    assert "generation_provenance" not in draft_page.text
    assert '<video src=' in draft_page.text
    assert playback_calls == []
    assert client.get(f"/api/remix/jobs/{job_id}/card?source=A&playback=true").status_code == 200
    assert client.get(f"/api/remix/jobs/{job_id}/card?source=A").status_code == 200
    assert client.get("/api/items").json()["items"] == before_save
    private_job = client.get(f"/api/remix/jobs/{job_id}").json()
    assert private_job["status"] == "ready"
    assert private_job["generation_identity"]["format"] == "remix-execution-v2"
    stranger = TestClient(create_app(settings))
    assert stranger.get(f"/api/remix/jobs/{job_id}/card").status_code == 200
    assert stranger.get(f"/api/remix/jobs/{job_id}/card?page=true").status_code == 200
    assert stranger.get(f"/api/remix/jobs/{job_id}/card?source=A").status_code == 200
    assert stranger.get(f"/api/remix/jobs/{job_id}/card?source=A&playback=true").status_code == 200
    with session_scope(settings) as session:
        session.get(RemixJobRecord, job_id).status = "queued"
    assert client.get(f"/api/remix/jobs/{job_id}/card").status_code == 409
    with session_scope(settings) as session:
        session.get(RemixJobRecord, job_id).status = "ready"

    save = client.post(f"/api/remix/jobs/{job_id}/save")
    assert save.status_code == 200
    saved = save.json()
    assert saved["status"] == "saved"
    assert saved["item"]["execution_identity"]["generation"]["format"] == "remix-execution-v2"
    assert saved["item"]["summary"].startswith("Ready generated remix")
    assert saved["item"]["public_status"] == "published"
    assert saved["item"]["visibility"] == "public"
    assert saved["item"]["origin"] == "remix"
    assert saved["item"]["saved_at"]
    assert saved["item"]["remix_tweaks"]["values"]["chaos"] == 68
    assert saved["item"]["remix_tweaks"]["schizo_level"] == 2

    post_save_items = client.get("/api/items").json()["items"]
    assert "PRIVATE_ROUTE_GUIDANCE_39" not in json.dumps(post_save_items)
    assert "execution_identity" not in json.dumps(post_save_items)
    assert any(item["id"] == saved["item"]["id"] for item in post_save_items)
    assert post_save_items[0]["id"] == saved["item"]["id"]
    assert post_save_items[0]["saved_at"] == saved["item"]["saved_at"]
    with session_scope(settings) as session:
        job = session.get(RemixJobRecord, job_id)
        assert job is not None
        assert job.status == "saved"
        assert job.saved_media_item_id == saved["item"]["id"]
        traces = session.scalars(
            select(RemixSourceRecord).where(RemixSourceRecord.remix_media_item_id == saved["item"]["id"])
        ).all()
        assert [trace.source_media_item_id for trace in traces] == media_ids[:2]

    share = client.post(f"/api/items/{saved['item']['id']}/share").json()
    assert share["media_url"] == f"{share['url']}/media"
    image_response = client.get(f"/api/items/{saved['item']['id']}/remix-card")
    assert image_response.status_code == 200
    image = Image.open(BytesIO(image_response.content))
    assert image.size == (1000, 1460)
    assert image_response.content == draft_card.content
    private_page = client.get(f"/api/items/{saved['item']['id']}/remix-card?page=true")
    assert private_page.status_code == 200
    assert "Saved settings" in private_page.text
    assert "frame-ancestors 'self'" in private_page.headers["content-security-policy"]
    share_path = urlsplit(share["url"]).path
    anonymous = TestClient(create_app(settings))
    assert anonymous.get(f"{share_path}/card").status_code == 404
    assert anonymous.get(f"{share_path}/card/image").status_code == 404
    assert anonymous.get(f"{share_path}/card/image?source=A").status_code == 404
    assert anonymous.get(f"{share_path}/card/image?source=A&playback=true").status_code == 404
    card_share = client.post(f"/api/items/{saved['item']['id']}/share?card=true").json()
    share_path = urlsplit(card_share["url"]).path
    card_page = anonymous.get(f"{share_path}/card")
    assert card_page.status_code == 200
    assert "PRIVATE_ROUTE_GUIDANCE_39" not in card_page.text
    assert "generation_provenance" not in card_page.text
    assert "Forward Deployed Meme" in card_page.text
    assert "The idea" in card_page.text
    assert "Amount of chaos" in card_page.text
    assert '<video src=' in card_page.text
    assert "frame-ancestors 'none'" in card_page.headers["content-security-policy"]
    assert f'{share_path}/card/image' in card_page.text
    public_image = anonymous.get(f"{share_path}/card/image")
    assert public_image.status_code == 200
    assert public_image.content == image_response.content
    assert "no-store" in public_image.headers["cache-control"]
    assert anonymous.get(f"{share_path}/card/image?source=A").status_code == 200
    assert anonymous.get(f"{share_path}/card/image?source=B").status_code == 200
    assert anonymous.get(f"{share_path}/card/image?source=C").status_code == 422
    playback = anonymous.get(f"{share_path}/card/image?source=A&playback=true", headers={"Range": "bytes=0-3"})
    assert playback.status_code == 206
    assert playback.headers["content-type"] == "video/mp4"
    assert len(playback.content) == 4
    assert playback_calls == [media_ids[0], media_ids[0], media_ids[0]]
    assert anonymous.get(f"{share_path}/card/image?source=B&playback=true").status_code == 404
    assert client.delete(f"/api/items/{saved['item']['id']}/share").status_code == 200
    assert anonymous.get(f"{share_path}/card").status_code == 404
    assert anonymous.get(f"{share_path}/card/image").status_code == 404
    assert anonymous.get(f"{share_path}/card/image?source=A").status_code == 404
    assert anonymous.get(f"{share_path}/card/image?source=A&playback=true").status_code == 404
    assert playback_calls == [media_ids[0], media_ids[0], media_ids[0]]
    assert client.get(f"/api/items/{saved['item']['id']}/remix-card").status_code == 200
    original_identity = saved["item"]["execution_identity"]
    saved_id = saved["item"]["id"]
    if metadata_change == "refresh":
        from meme_arsenal.ai import classify_media_item
        current = replace(settings, classification_style_guidance="new classification style")
        with session_scope(current) as session:
            item = session.get(MediaItem, saved_id)
            row = session.scalar(select(Classification).where(
                Classification.media_item_id == saved_id,
                Classification.model == settings.ai_model,
                Classification.prompt_version == settings.classification_prompt_version,
            ))
            assert row is not None
            refreshed = classify_media_item(session, current, item)
            assert refreshed is row
            assert row.compatibility_key != original_identity["classification"]["compatibility_key"]
            assert "generation" not in row.raw_response_json
            expected_classification = {**original_identity["classification"],
                                       "compatibility_key": row.compatibility_key}
    else:
        edited = client.patch(f"/api/items/{saved_id}/metadata", json={"summary": "Owner edited"})
        assert edited.status_code == 200
        expected_classification = None
    job_response = client.get(f"/api/remix/jobs/{job_id}")
    repeated_save = client.post(f"/api/remix/jobs/{job_id}/save")
    assert job_response.status_code == repeated_save.status_code == 200
    for payload in (job_response.json()["saved_item"], repeated_save.json()["item"]):
        assert payload["execution_identity"] == {
            "generation": original_identity["generation"], "classification": expected_classification,
        }
    assert client.delete(f"/api/remix/items/{saved['item']['id']}").status_code == 204
    assert client.get(urlsplit(share["url"]).path).status_code == 404
    assert client.get(f"/api/items/{saved['item']['id']}").status_code == 404
    assert client.get(f"/api/remix/jobs/{job_id}/card").status_code == 409
    with session_scope(settings) as session:
        job = session.get(RemixJobRecord, job_id)
        assert job is not None
        assert job.status == "deleted"
        assert job.saved_media_item_id is None


def test_online_api_upload_accepts_windows_named_jpeg_and_mp4(settings, tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")

    settings = _online_settings(settings)
    settings = replace(settings, database_url=f"sqlite:///{tmp_path}/loader.db")
    with session_scope(settings) as db:
        db.add(AnonymousSession(id=settings.owner_session_id))
    client = TestClient(create_app(settings))

    sample_dir = tmp_path / "loader_samples"
    sample_dir.mkdir()
    image_path = sample_dir / "upload-check.jpg"
    video_path = sample_dir / "upload-check.mp4"
    Image.new("RGB", (56, 56), "green").save(image_path)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=64x64:rate=12",
            str(video_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    samples = [
        ("C:\\\\Users\\\\fixture-user\\\\OneDrive\\\\Pictures\\\\sample-image.jpg", image_path),
        ("C:\\\\Users\\\\fixture-user\\\\OneDrive\\\\Pictures\\\\sample-video.mp4", video_path),
    ]

    for filename, source in samples:
        response = client.post(
            "/api/remix/uploads",
            files={"file": (filename, source.read_bytes(), "application/octet-stream")},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["origin"] == "dashboard_upload"
        assert payload["public_status"] == "draft"
        assert payload["visibility"] == "private"
        assert payload["processing_status"] == "processed"
        assert payload["original_path"] is not None
        assert payload["original_path"].startswith("/media/originals/")
        # A new browser with no old cookie recovers standalone image/video drafts.
        recovered = TestClient(create_app(settings))
        assert payload["id"] in {item["id"] for item in recovered.get("/api/remix/uploads").json()["drafts"]}
        assert recovered.get(f"/api/remix/uploads/{payload['id']}").json()["id"] == payload["id"]
        assert recovered.get(payload["original_path"]).status_code == 200
        client = recovered
        if payload["media_type"] == "video":
            assert payload["playback_path"] == f"/api/items/{payload['id']}/playback/mobile-v2"
            playback = client.get(payload["playback_path"], headers={"Range": "bytes=0-4095"})
            assert playback.status_code == 206
            assert playback.headers["content-type"].startswith("video/mp4")
            assert playback.headers["cache-control"] == "private, no-store"
            assert playback.content.find(b"moov") < playback.content.find(b"mdat")
            cached = settings.video_playback_dir / f"media-{payload['id']:08d}-mobile-v2.mp4"
            assert cached.exists()
            assert cached.stat().st_size > 0
            assert client.post(f"/api/items/{payload['id']}/share").status_code == 404
            saved = client.post(f"/api/remix/uploads/{payload['id']}/save")
            assert saved.status_code == 200
            assert saved.json()["status"] == "saved"
            assert saved.json()["item"]["public_status"] == "published"
            assert saved.json()["item"]["visibility"] == "public"
            shared = client.post(f"/api/items/{payload['id']}/share")
            assert shared.status_code == 200
            share = shared.json()
            share_path = urlsplit(share["url"]).path
            share_page = client.get(share_path)
            assert share_page.status_code == 200
            assert "playsinline" in share_page.text
            assert "disablepictureinpicture" in share_page.text
            assert f"{share_path}/media?v=2" in share_page.text
            shared_playback = client.get(f"{share_path}/media", headers={"Range": "bytes=0-4095"})
            assert shared_playback.status_code == 206
            assert shared_playback.headers["content-type"].startswith("video/mp4")
            assert shared_playback.content.find(b"moov") < shared_playback.content.find(b"mdat")
            assert client.delete(f"/api/items/{payload['id']}/share").status_code == 200
        else:
            assert payload["playback_path"] is None
            saved = client.post(f"/api/remix/uploads/{payload['id']}/save")
            assert saved.status_code == 200
            assert saved.json()["item"]["public_status"] == "published"
