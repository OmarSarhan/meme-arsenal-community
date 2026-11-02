from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from meme_arsenal.config import load_settings, validate_owner_id
from meme_arsenal.db import session_scope
from meme_arsenal.models import AnonymousSession, RemixJobRecord
from meme_arsenal.online import create_app
from meme_arsenal.owner import inventory, provision, require_owner
from meme_arsenal.online_worker import _claim_remix_job, _recover_stale_jobs, _next_queued_job


def test_exact_file_and_conflicts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    value = "MiX: /雪 internal space"
    path = tmp_path / "owner"
    path.write_text(value + "\n")
    monkeypatch.setenv("OWNER_SESSION_ID_FILE", str(path))
    assert load_settings().owner_session_id == value
    monkeypatch.setenv("OWNER_SESSION_ID", "different")
    with pytest.raises(ValueError, match="Conflicting"):
        load_settings()
    path.unlink()
    with pytest.raises(ValueError, match="OWNER_SESSION_ID_FILE"):
        load_settings()
    assert load_settings(ignore_owner=True).owner_session_id is None


@pytest.mark.parametrize("value", [None, "", " x", "x ", "x\r", "x\n", "x\x00", "x" * 65])
def test_invalid_owner(value):
    with pytest.raises(ValueError):
        validate_owner_id(value)


def test_inventory_and_adoption_preserve_namespaces(settings):
    with session_scope(settings) as db:
        db.add_all([AnonymousSession(id="B"), AnonymousSession(id=" unsupported ")])
    before = inventory(settings)
    assert provision(settings, initialize=False).startswith("Verified")
    assert provision(settings, initialize=True).startswith("Verified")
    assert inventory(settings) == before
    assert any(not row["adoptable"] for row in before["namespaces"])
    absent = replace(settings, owner_session_id="new")
    with pytest.raises(ValueError):
        require_owner(absent)
    with pytest.raises(ValueError):
        provision(absent, initialize=False)
    with pytest.raises(ValueError):
        provision(absent, initialize=True)
    provision(absent, initialize=True, allow_empty_namespace=True)
    assert require_owner(absent) == "new"


def test_inventory_missing_schema_does_not_create_layout(settings, tmp_path):
    path = tmp_path / "empty.sqlite"
    path.touch()
    candidate = replace(settings, database_url=f"sqlite:///{path}", reserve_path=tmp_path / "absent")
    with pytest.raises(Exception):
        inventory(candidate)
    assert not candidate.reserve_path.exists()
    from sqlalchemy import create_engine, inspect
    assert inspect(create_engine(candidate.database_url)).get_table_names() == []


def test_populated_adoption_and_key_rotation_preserve_owner_and_excluded_data(settings, fixture_dir):
    from sqlalchemy import select
    from meme_arsenal.media import process_pending_media
    from meme_arsenal.models import (
        AnonymousSessionCuration, Curation, MediaItem, MediaTag, RemixSourceRecord, TelegramMessage,
    )
    from meme_arsenal.online import refresh_public_indexes
    from meme_arsenal.telegram_importer import import_fixture_messages

    settings = replace(settings, public_session_cookie_secure=False, redis_url="redis://localhost:1/0")
    with session_scope(settings) as db:
        import_fixture_messages(db, settings, fixture_dir)
        process_pending_media(db, settings)
        refresh_public_indexes(db, settings=settings)
        ids = list(db.scalars(select(MediaItem.id).order_by(MediaItem.id)))
        db.add(AnonymousSession(id="B"))
        db.add(Curation(media_item_id=ids[0], manual_tags_json=["global-tag"], notes="global-note"))
        db.add(MediaTag(media_item_id=ids[0], tag_type="topics", value="global-topic", source="manual"))
        db.add(AnonymousSessionCuration(session_id="B", media_item_id=ids[0], notes="excluded-note"))
    owner = TestClient(create_app(settings))
    curation = {"favorite": True, "archived": True, "manual_tags": ["owner-tag"], "notes": "owner-note"}
    assert owner.patch(f"/api/session/items/{ids[0]}", json=curation).status_code == 200
    image = (fixture_dir / "deploy-fails.png").read_bytes()
    own_draft = owner.post("/api/remix/uploads", files={"file": ("a.png", image, "image/png")}).json()
    other = TestClient(create_app(replace(settings, owner_session_id="B")))
    foreign_draft = other.post("/api/remix/uploads", files={"file": ("b.png", image, "image/png")}).json()
    with session_scope(settings) as db:
        for job_id, namespace in (("owner-job", settings.owner_session_id), ("excluded-job", "B"), ("null-job", None)):
            db.add(RemixJobRecord(id=job_id, session_id=namespace, left_id=ids[0], right_id=ids[1],
                                  status="ready", saved_media_item_id=ids[1]))
        db.flush()
        db.add(RemixSourceRecord(job_id="owner-job", remix_media_item_id=ids[1], source_media_item_id=ids[0], position=0))

    def snapshot():
        with session_scope(settings) as db:
            return {model.__tablename__: db.execute(select(model.__table__).order_by(model.id)).all()
                    for model in (Curation, AnonymousSessionCuration, MediaItem, TelegramMessage, MediaTag,
                                  RemixJobRecord, RemixSourceRecord)}

    before = snapshot()
    before_inventory = inventory(settings)
    assert provision(settings, initialize=False).startswith("Verified")
    assert inventory(settings) == before_inventory
    # Both fresh browser clients and signing-key rotation retain the selected namespace.
    for browser_settings in (settings, replace(settings, public_session_secret="new-synthetic-signing-key")):
        browser = TestClient(create_app(browser_settings))
        detail = browser.get(f"/api/items/{ids[0]}").json()
        assert {key: detail[key] for key in curation} == curation
        assert detail["server_tags"] == ["global-tag"]
        assert browser.get(f"/api/remix/uploads/{own_draft['id']}").status_code == 200
        assert browser.get(f"/api/remix/uploads/{foreign_draft['id']}").status_code == 404
        assert browser.get("/api/remix/jobs/owner-job?include_sources=true").status_code == 200
        assert browser.get("/api/remix/jobs/excluded-job").status_code == 404
        assert browser.get("/api/remix/jobs/null-job").status_code == 404
    assert snapshot() == before
    with session_scope(settings) as db:
        assert set(db.scalars(select(AnonymousSession.id))) == {settings.owner_session_id, "B"}


def test_job_keyset_cookie_loss_and_foreign_worker(settings):
    from datetime import datetime, timedelta, timezone
    stamp = datetime.now(timezone.utc) - timedelta(days=1)
    with session_scope(settings) as db:
        db.add(AnonymousSession(id="B"))
        for index in range(45):
            db.add(RemixJobRecord(id=f"job-{index:03}", session_id=settings.owner_session_id,
                                 left_id=1, right_id=2, status="ready" if index == 0 else "failed", created_at=stamp))
        for id, owner in (("foreign", "B"), ("null", None)):
            db.add(RemixJobRecord(id=id, session_id=owner, left_id=1, right_id=2, status="queued"))
    client = TestClient(create_app(settings))
    ids = []
    cursor = None
    while True:
        page = client.get("/api/remix/jobs", params={"limit": 20, **({"cursor": cursor} if cursor else {})}).json()
        ids.extend(job["id"] for job in page["jobs"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(ids) == len(set(ids)) == 45 and ids[-1] == "job-000"
    client.cookies.set(settings.public_session_cookie_name, "forged")
    assert client.get("/api/remix/jobs/job-000").status_code == 200
    from itsdangerous import URLSafeSerializer
    signer = URLSafeSerializer(settings.public_session_secret, salt="meme-arsenal-public-session")
    client.cookies.set(settings.public_session_cookie_name, signer.dumps({"sid": "B"}))
    assert client.get("/api/remix/jobs/job-000").status_code == 200
    rotated = TestClient(create_app(replace(settings, public_session_secret="rotated-synthetic-key")))
    rotated.cookies.set(settings.public_session_cookie_name, signer.dumps({"sid": "B"}))
    assert rotated.get("/api/remix/jobs/job-000").status_code == 200
    assert client.get("/api/remix/jobs/foreign").status_code == 404
    assert _claim_remix_job(settings, "foreign") == "missing"
    assert _claim_remix_job(settings, "null") == "missing"
    assert _next_queued_job(settings) is None
    assert _recover_stale_jobs(settings) == 0
    with session_scope(settings) as db:
        assert db.get(RemixJobRecord, "foreign").status == "queued"


def test_standalone_drafts_and_foreign_nested_sources(settings, fixture_dir, monkeypatch):
    from meme_arsenal.models import RemixSourceRecord, RemixAttemptRecord
    from meme_arsenal.online import _remix_trace_maps
    from meme_arsenal.online_worker import process_remix_job
    from datetime import datetime, timedelta, timezone
    other = replace(settings, owner_session_id="B", public_session_cookie_secure=False)
    provision(other, initialize=True, allow_empty_namespace=True)
    settings = replace(settings, public_session_cookie_secure=False)
    a, b = TestClient(create_app(settings)), TestClient(create_app(other))
    image = (fixture_dir / "deploy-fails.png").read_bytes()
    def upload(client):
        response = client.post("/api/remix/uploads", files={"file": ("fixture.png", image, "image/png")})
        assert response.status_code == 200
        return response.json()
    own, foreign = upload(a), upload(b)
    assert [row["id"] for row in a.get("/api/remix/uploads").json()["drafts"]] == [own["id"]]
    assert a.get(f"/api/remix/uploads/{foreign['id']}").status_code == 404
    assert a.get(foreign["original_path"]).status_code == 404
    assert a.get("/api/items").json()["items"] == []
    with session_scope(settings) as db:
        db.add(RemixSourceRecord(remix_media_item_id=own["id"], source_media_item_id=foreign["id"], position=0))
        db.add(RemixJobRecord(id="invalid-input", session_id=settings.owner_session_id,
                             left_id=own["id"], right_id=foreign["id"], status="queued"))
        db.add(RemixJobRecord(id="foreign-stale", session_id="B", left_id=own["id"], right_id=foreign["id"],
                             status="calling_gemini", updated_at=datetime.now(timezone.utc) - timedelta(hours=2)))
        db.flush()
        db.add(RemixAttemptRecord(job_id="foreign-stale", attempt=1, status="running"))
    with session_scope(settings) as db:
        sources, used_by = _remix_trace_maps(db, settings, [own["id"], foreign["id"]], session_id=settings.owner_session_id)
        assert sources[own["id"]] == []
        assert used_by[foreign["id"]][0]["id"] == own["id"]
    def forbidden(*args, **kwargs):
        pytest.fail("Foreign input reached mitigation/provider")
    monkeypatch.setattr("meme_arsenal.online_worker.build_remix_mitigation_plan", forbidden)
    monkeypatch.setattr("meme_arsenal.online_worker.generate_remix", forbidden)
    assert process_remix_job(settings, "invalid-input")["status"] == "failed"
    assert _recover_stale_jobs(settings) == 0
    with session_scope(settings) as db:
        assert db.get(RemixJobRecord, "foreign-stale").attempts[0].status == "running"
    with session_scope(settings) as db:
        from meme_arsenal.models import MediaItem, TelegramMessage
        item = db.get(MediaItem, own["id"])
        item.origin, item.public_status, item.visibility = "remix", "published", "public"
        db.get(TelegramMessage, item.telegram_message_id).raw_metadata_json = {
            "remix_context": {"ingredients": [{"side": "A", "summary": "DENIED SNAPSHOT"}]}}
        db.add(RemixSourceRecord(remix_media_item_id=own["id"], source_media_item_id=own["id"], position=1))
    recipe = f"/api/items/{own['id']}/remix-card"
    assert a.get(recipe + "?source=A").status_code == 404
    assert a.get(recipe + "?source=B").status_code == 200
    page = a.get(recipe + "?page=true")
    assert page.status_code == 200 and "DENIED SNAPSHOT" not in page.text
