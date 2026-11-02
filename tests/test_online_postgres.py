from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading

from fastapi.testclient import TestClient
from sqlalchemy import select

from meme_arsenal.db import session_scope
from meme_arsenal.media import process_pending_media
from meme_arsenal.models import AnonymousSession
from meme_arsenal.online import create_app, refresh_public_indexes
from meme_arsenal.telegram_importer import import_fixture_messages


def _workflow_fixture(settings, fixture_dir, monkeypatch, tmp_path):
    from oma27_worker import install_stubs
    from test_online_worker import _gemini_image_response
    settings = replace(settings, ai_provider="google", google_api_key="test-fixture-only",
                       ai_model="mock-classifier", online_embeddings_enabled=False,
                       public_session_cookie_secure=False)
    log = tmp_path / "calls.log"
    log.touch()
    response = _gemini_image_response()
    install_stubs(monkeypatch.setattr, log, response, "forbidden")
    client = TestClient(create_app(settings))
    ids = []
    for name in ("deploy-fails.png", "smug-reaction.png"):
        upload = client.post("/api/remix/uploads", files={
            "file": (name, (fixture_dir / name).read_bytes(), "image/png")})
        assert upload.status_code == 200, upload.text
        ids.append(upload.json()["id"])
    created = client.post("/api/remix/jobs", json={"left_id": ids[0], "right_id": ids[1],
        "tweaks": {"summary": "Retain recovery settings", "controls": [], "values": {},
                   "no_text": True, "anti_slop": True}})
    assert created.status_code == 202, created.text
    return settings, client, ids, created.json()["id"], log, response


def _assert_saved_identity(settings, job_id, ids, saved_id):
    from meme_arsenal.models import MediaItem, RemixJobRecord, RemixSourceRecord, TelegramMessage
    with session_scope(settings) as db:
        job = db.get(RemixJobRecord, job_id)
        assert job.status == "saved"
        assert job.saved_media_item_id == saved_id
        messages = [m for m in db.scalars(select(TelegramMessage))
                    if (m.raw_metadata_json or {}).get("remix_job_id") == job_id]
        assert len(messages) == 1
        outputs = list(db.scalars(select(MediaItem).where(
            MediaItem.telegram_message_id == messages[0].id)))
        assert [m.id for m in outputs] == [saved_id]
        links = list(db.scalars(select(RemixSourceRecord).where(
            RemixSourceRecord.job_id == job_id).order_by(RemixSourceRecord.position)))
        assert [(r.source_media_item_id, r.position, r.remix_media_item_id) for r in links] == [
            (ids[0], 0, saved_id), (ids[1], 1, saved_id)]


def test_remix_save_collision_retains_lock_and_is_idempotent(
    postgres_settings, fixture_dir, monkeypatch, tmp_path,
):
    import pytest
    from sqlalchemy import event, text
    from sqlalchemy.exc import OperationalError
    from meme_arsenal import remix
    from meme_arsenal.models import RemixJobRecord, TelegramMessage
    from meme_arsenal.online_worker import run_worker
    settings, client, ids, job_id, log, response = _workflow_fixture(
        postgres_settings, fixture_dir, monkeypatch, tmp_path)
    from oma27_worker import install_stubs
    install_stubs(monkeypatch.setattr, log, response, "generate")
    run_worker(settings, once=True)
    with session_scope(settings) as db:
        db.add(TelegramMessage(telegram_message_id=-12345, chat_id=remix.REMIX_CHAT_ID,
                               caption="committed collision", date=datetime.now(timezone.utc)))
    # Bound every request transaction, including the waiting second save.
    app = create_app(settings)
    engine = app.state.runtime.session_local.kw["bind"]
    def limits(connection):
        connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
        connection.exec_driver_sql("SET LOCAL statement_timeout = '10s'")
    event.listen(engine, "begin", limits)
    entered, release = threading.Event(), threading.Event()
    calls = []
    next_id = remix._next_remix_message_id
    process = remix.process_media_item
    def candidate(db, **kwargs):
        calls.append(1)
        return -12345 if len(calls) == 1 else next_id(db, **kwargs)
    def pause(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return process(*args, **kwargs)
    monkeypatch.setattr(remix, "_next_remix_message_id", candidate)
    monkeypatch.setattr(remix, "process_media_item", pause)
    pool = ThreadPoolExecutor(max_workers=2)
    first = pool.submit(TestClient(app).post, f"/api/remix/jobs/{job_id}/save")
    try:
        assert entered.wait(10)
        with pytest.raises(OperationalError) as error:
            with session_scope(settings) as db:
                db.execute(text("SET LOCAL statement_timeout = '2s'"))
                db.scalar(select(RemixJobRecord).where(
                    RemixJobRecord.id == job_id).with_for_update(nowait=True))
        assert error.value.orig.sqlstate == "55P03"
        second = pool.submit(TestClient(app).post, f"/api/remix/jobs/{job_id}/save")
    finally:
        release.set()
        pool.shutdown(wait=True)
        event.remove(engine, "begin", limits)
    results = [first.result(timeout=15), second.result(timeout=15)]
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    saved_id = results[0].json()["item"]["id"]
    assert results[1].json()["item"]["id"] == saved_id
    replay = TestClient(create_app(settings)).post(f"/api/remix/jobs/{job_id}/save")
    assert replay.status_code == 200
    assert replay.json()["item"]["id"] == saved_id
    assert len(calls) == 2
    _assert_saved_identity(settings, job_id, ids, saved_id)
    assert "UNEXPECTED" not in log.read_text()


def test_modern_workflow_survives_worker_process_loss(
    postgres_settings, fixture_dir, monkeypatch, tmp_path,
):
    import hashlib
    import json
    from pathlib import Path
    import subprocess
    import sys
    import time
    from sqlalchemy.engine import make_url
    from meme_arsenal.models import MediaItem, RemixJobRecord
    settings, client, ids, job_id, log, response = _workflow_fixture(
        postgres_settings, fixture_dir, monkeypatch, tmp_path)
    schema = make_url(settings.database_url).query["options"].split("=", 1)[1].split(",")[0]
    payload = tmp_path / "worker.json"
    payload.write_text(json.dumps({"schema": schema, "log": str(log), "response": response,
        "settings": {"database_url": settings.database_url, "reserve_path": str(settings.reserve_path),
                     "owner_session_id": settings.owner_session_id, "ai_provider": "google",
                     "google_api_key": "test-fixture-only", "ai_model": "mock-classifier",
                     "embedding_model": "mock-embedding", "online_embeddings_enabled": False}}))
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).resolve().parents[1]
    env = {"HOME": str(home), "TMPDIR": str(tmp_path), "PATH": "/usr/bin:/bin",
           "PYTHONPATH": str(source / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    children = []
    def launch(mode):
        child = subprocess.Popen([sys.executable, str(source / "tests/oma27_worker.py"),
                                  str(payload), mode], cwd=home, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children.append(child)
        return child
    def complete(mode):
        child = launch(mode)
        out, err = child.communicate(timeout=30)
        assert child.returncode == 0, out + err
    def snapshot():
        with session_scope(settings) as db:
            job = db.get(RemixJobRecord, job_id)
            return (job.status, job.result_json, job.tweaks_json, job.left_id, job.right_id,
                    job.saved_media_item_id,
                    [(a.attempt, a.status) for a in sorted(job.attempts, key=lambda a: a.attempt)])
    try:
        child = launch("blocked")
        deadline = time.monotonic() + 20
        while not log.with_suffix(".barrier").exists():
            assert child.poll() is None, child.communicate(timeout=2)
            assert time.monotonic() < deadline, "generation barrier timeout"
            time.sleep(0.05)
        running = snapshot()
        assert running[0] == "calling_gemini"
        assert running[-1] == [(1, "running")]
        child.kill()
        child.communicate(timeout=10)
        assert child.returncode == -9
        complete("forbidden")
        assert snapshot() == running
        with session_scope(settings) as db:
            # Accelerate only this killed fixture's existing stale-age policy.
            db.get(RemixJobRecord, job_id).updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
        complete("generate")
        ready = snapshot()
        assert ready[0] == "ready"
        assert ready[-1] == [(1, "interrupted"), (2, "succeeded")]
        assert ready[3:5] == tuple(ids)
        draft = settings.remix_drafts_dir / ready[1]["result"]["image_path"]
        digest = hashlib.sha256(draft.read_bytes()).hexdigest()
        calls = log.read_text()
        assert calls.splitlines() == ["generation blocked", "generation generate"]
        complete("forbidden")
        assert snapshot() == ready
        assert hashlib.sha256(draft.read_bytes()).hexdigest() == digest
        saved = TestClient(create_app(settings)).post(f"/api/remix/jobs/{job_id}/save")
        assert saved.status_code == 200, saved.text
        saved_id = saved.json()["item"]["id"]
        state = snapshot()
        with session_scope(settings) as db:
            media = db.get(MediaItem, saved_id)
            original = settings.reserve_path / media.original_path
            original_digest = hashlib.sha256(original.read_bytes()).hexdigest()
        complete("forbidden")
        assert snapshot() == state
        assert hashlib.sha256(original.read_bytes()).hexdigest() == original_digest
        replay = TestClient(create_app(settings)).post(f"/api/remix/jobs/{job_id}/save")
        assert replay.status_code == 200
        assert replay.json()["item"] == saved.json()["item"]
        _assert_saved_identity(settings, job_id, ids, saved_id)
        assert log.read_text() == calls
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
        payload.unlink(missing_ok=True)


def test_owner_claim_once_and_stale_completion(postgres_settings, fixture_dir):
    from meme_arsenal.models import RemixJobRecord, RemixAttemptRecord
    from meme_arsenal.online_worker import _claim_remix_job, _recover_stale_jobs
    settings = replace(postgres_settings, remix_provider="google")
    with session_scope(settings) as db:
        import_fixture_messages(db, settings, fixture_dir)
        db.add(RemixJobRecord(id="claim", session_id=settings.owner_session_id,
                              left_id=1, right_id=2, status="queued"))
    barrier = threading.Barrier(2)
    def claim():
        barrier.wait(timeout=5)
        return _claim_remix_job(settings, "claim")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        assert sorted(f.result(timeout=10) for f in futures) == ["claimed", "preparing_media"]
    with session_scope(settings) as db:
        job = db.get(RemixJobRecord, "claim")
        job.status = "calling_gemini"
        job.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db.add(RemixAttemptRecord(job_id="claim", attempt=1, status="running"))
    # Completion owns the row lock before recovery: SKIP LOCKED must leave its
    # attempt intact, and a committed ready row must never be reset afterwards.
    with session_scope(settings) as db:
        job = db.scalar(select(RemixJobRecord).where(RemixJobRecord.id == "claim").with_for_update())
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(_recover_stale_jobs, settings).result(timeout=5) == 0
        job.status = "ready"
        job.attempts[0].status = "succeeded"
    assert _recover_stale_jobs(settings) == 0
    with session_scope(settings) as db:
        assert db.get(RemixJobRecord, "claim").attempts[0].status == "succeeded"


def test_owner_cap_serializes_browsers(postgres_settings, fixture_dir):
    from meme_arsenal.models import MediaItem, RemixJobRecord
    settings = replace(postgres_settings, ai_provider="google", google_api_key="fixture-only",
                       public_session_cookie_secure=False, redis_url="redis://localhost:1/0")
    with session_scope(settings) as db:
        import_fixture_messages(db, settings, fixture_dir)
        process_pending_media(db, settings)
        ids = list(db.scalars(select(MediaItem.id).order_by(MediaItem.id)))
        db.add(RemixJobRecord(id="existing", session_id=settings.owner_session_id,
                              left_id=ids[0], right_id=ids[1], status="queued"))
    app = create_app(settings)
    barrier = threading.Barrier(2)
    def create():
        client = TestClient(app)
        barrier.wait(timeout=5)
        return client.post("/api/remix/jobs", json={"left_id": ids[0], "right_id": ids[1]}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(create) for _ in range(2)]
        assert sorted(result.result(timeout=20) for result in results) == [202, 429]


def test_owner_upload_numbering_serializes_browsers(postgres_settings, fixture_dir):
    from meme_arsenal.models import TelegramMessage
    settings = replace(postgres_settings, public_session_cookie_secure=False, redis_url="redis://localhost:1/0")
    app = create_app(settings)
    image = (fixture_dir / "deploy-fails.png").read_bytes()
    barrier = threading.Barrier(2)
    def upload():
        client = TestClient(app)
        barrier.wait(timeout=5)
        return client.post("/api/remix/uploads", files={"file": ("fixture.png", image, "image/png")})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(upload) for _ in range(2)]
        assert all(result.result(timeout=20).status_code == 200 for result in results)
    with session_scope(settings) as db:
        numbers = list(db.scalars(select(TelegramMessage.telegram_message_id).where(
            TelegramMessage.chat_id == f"dashboard-upload:{settings.owner_session_id}")))
        assert len(numbers) == len(set(numbers)) == 2


def test_analysis_does_not_block_gallery_in_the_same_session(postgres_settings, fixture_dir, monkeypatch):
    settings = replace(postgres_settings, public_session_cookie_secure=False,
                       public_session_secret="test-short-session-transaction", redis_url="redis://localhost:1/0")
    with session_scope(settings) as db:
        import_fixture_messages(db, settings, fixture_dir)
        process_pending_media(db, settings)
        refresh_public_indexes(db, settings=settings)
    settings = replace(settings, ai_provider="google", google_api_key="fake-analysis-key")
    app = create_app(settings)
    client = TestClient(app)
    rows = client.get("/api/items").json()["items"]
    entered, release = threading.Event(), threading.Event()

    class Analysis:
        def payload(self):
            return {"controls": []}

    def slow_analysis(*args):
        entered.set()
        assert release.wait(10)
        return Analysis()

    monkeypatch.setattr("meme_arsenal.online.analyze_remix_tweaks", slow_analysis)
    with ThreadPoolExecutor(max_workers=2) as pool:
        analysis = pool.submit(client.post, "/api/remix/analysis", json={
            "left_id": rows[0]["id"], "right_id": rows[1]["id"]
        })
        try:
            assert entered.wait(5)
            gallery = pool.submit(client.get, "/api/items", params={"q": "deploy"})
            assert gallery.result(timeout=2).status_code == 200
        finally:
            release.set()
        assert analysis.result(timeout=5).status_code == 200


def test_busy_session_lock_does_not_block_heartbeat(postgres_settings):
    settings = replace(postgres_settings, public_session_cookie_secure=False,
                       public_session_secret="test-skip-locked-heartbeat", redis_url="redis://localhost:1/0")
    with session_scope(settings):
        pass
    app = create_app(settings)
    client = TestClient(app)
    assert client.get("/api/items").status_code == 200
    with session_scope(settings) as db:
        session = db.scalar(select(AnonymousSession))
        session.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with app.state.runtime.session_local.begin() as db:
            db.scalar(select(AnonymousSession).with_for_update())
            assert pool.submit(client.get, "/api/items").result(timeout=2).status_code == 200
