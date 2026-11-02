"""Capture/claim boundaries; all generation transport must be stubbed."""
from dataclasses import replace
from threading import Event

import pytest
from sqlalchemy import select

from meme_arsenal.db import session_scope
from meme_arsenal.models import MediaItem, RemixJobRecord, RemixAttemptRecord
from meme_arsenal.remix_execution import capture_execution
from meme_arsenal import online_worker as worker
from test_online_worker import _prepare_archive, _create_worker_job, _gemini_image_response


def queued(settings, fixture_dir):
    _prepare_archive(settings, fixture_dir)
    with session_scope(settings) as db:
        ids = list(db.scalars(select(MediaItem.id)))
    return _create_worker_job(settings, *ids)


@pytest.mark.parametrize("raw", [{}, {"format": "unknown"}, "secret-invalid-json"])
def test_invalid_terminal_even_when_paused(settings, fixture_dir, monkeypatch, raw):
    job_id = queued(settings, fixture_dir)
    with session_scope(settings) as db:
        db.get(RemixJobRecord, job_id).execution_capture_json = raw
    monkeypatch.setattr(worker, "_ai_work_paused", lambda _: pytest.fail("invalid must precede gate"))
    assert worker.process_remix_job(settings, job_id)["status"] == "failed"
    with session_scope(settings) as db:
        job = db.get(RemixJobRecord, job_id)
        assert job.error == "Remix configuration is incompatible. Create a new job."
        assert job.attempts == []


def test_stop_and_pause_do_not_adopt_legacy(settings, fixture_dir, monkeypatch):
    settings = replace(settings, remix_provider="google")
    job_id = queued(settings, fixture_dir)
    stop = Event()
    def gate(_):
        stop.set()
        return False
    monkeypatch.setattr(worker, "_ai_work_paused", gate)
    assert worker.process_remix_job(settings, job_id, stop=stop)["status"] == "queued"
    monkeypatch.setattr(worker, "_ai_work_paused", lambda _: True)
    assert worker.process_remix_job(settings, job_id)["paused"]
    with session_scope(settings) as db:
        job = db.get(RemixJobRecord, job_id)
        assert job.execution_capture_json is None
        assert job.execution_first_captured_attempt is None


def test_legacy_boundary_and_loser_preserve_winner(settings, fixture_dir):
    settings = replace(settings, remix_provider="google")
    job_id = queued(settings, fixture_dir)
    with session_scope(settings) as db:
        db.add(RemixAttemptRecord(job_id=job_id, attempt=3, status="interrupted"))
    candidate = worker.owned_job_execution(settings, job_id)
    other = worker.owned_job_execution(replace(settings, remix_model="other"), job_id)
    assert worker._claim_remix_job(settings, job_id, candidate) == "claimed"
    assert worker._claim_remix_job(settings, job_id, other) == "preparing_media"
    assert worker._invalidate_queued_job(settings, job_id, other) == "preparing_media"
    with session_scope(settings) as db:
        job = db.get(RemixJobRecord, job_id)
        assert job.execution_capture_json == candidate.capture.payload()
        assert job.execution_first_captured_attempt == 4
        from meme_arsenal.models import AnonymousSession
        db.add(AnonymousSession(id="foreign"))
    assert worker.owned_job_execution(replace(settings, owner_session_id="foreign"), job_id).status == "missing"


def test_direct_rejects_untyped_capture_before_media(settings, monkeypatch):
    from meme_arsenal.remix import generate_remix, RemixUnavailableError
    with pytest.raises(RemixUnavailableError, match="Create a new job"):
        generate_remix(None, settings, 1, 2, execution_capture={"provider": "google"})


def test_missing_current_key_is_not_recovered(settings, fixture_dir, monkeypatch):
    original = replace(settings, remix_provider="google", google_api_key="old-key")
    job_id = queued(original, fixture_dir)
    with session_scope(settings) as db:
        job = db.get(RemixJobRecord, job_id)
        job.execution_capture_json = capture_execution(original, "new_job").payload()
        job.execution_first_captured_attempt = 1
        assert "old-key" not in str(job.execution_capture_json)
    monkeypatch.setattr("meme_arsenal.remix.post_google_model", lambda *a: pytest.fail("missing key reached provider"))
    assert worker.process_remix_job(replace(original, google_api_key=None), job_id)["status"] == "failed"


def test_source_revoked_after_capture(settings, fixture_dir, monkeypatch):
    settings = replace(settings, remix_provider="google", google_api_key="dummy")
    job_id = queued(settings, fixture_dir)
    with session_scope(settings) as db:
        job = db.get(RemixJobRecord, job_id)
        job.execution_capture_json = capture_execution(settings, "new_job").payload()
        job.execution_first_captured_attempt = 1
        db.get(MediaItem, job.left_id).visibility = "private"
    monkeypatch.setattr(worker, "generate_remix", lambda *a, **k: pytest.fail("revoked source reached generation"))
    assert worker.process_remix_job(settings, job_id)["status"] == "failed"


@pytest.mark.parametrize("change", ["policy", "boundary", "legacy-config"])
def test_incompatible_identity_is_terminal(settings, fixture_dir, monkeypatch, change):
    settings = replace(settings, remix_provider="google")
    job_id = queued(settings, fixture_dir)
    with session_scope(settings) as db:
        job = db.get(RemixJobRecord, job_id)
        if change != "legacy-config":
            raw = capture_execution(settings, "new_job").payload()
            if change == "policy":
                raw["policy_digest"] = "0" * 64
            job.execution_capture_json = raw
            job.execution_first_captured_attempt = 9 if change == "boundary" else 1
    if change == "legacy-config":
        settings = replace(settings, remix_model="")
    monkeypatch.setattr(worker, "_ai_work_paused", lambda _: pytest.fail("invalid gated"))
    assert worker.process_remix_job(settings, job_id)["status"] == "failed"


def test_retained_capture_at_actual_request(settings, fixture_dir, monkeypatch):
    original = replace(settings, remix_provider="google", remix_model="retained-image",
                       remix_aspect_ratio="1:1", google_api_key="old-dummy", remix_style_guidance="retained A")
    job_id = queued(original, fixture_dir)
    capture = capture_execution(original, "new_job")
    with session_scope(settings) as db:
        job = db.get(RemixJobRecord, job_id)
        job.execution_capture_json = capture.payload()
        job.execution_first_captured_attempt = 1
    current = replace(settings, ai_provider="mock", remix_model="changed-image",
                      remix_aspect_ratio="16:9", google_api_key="current-dummy", remix_style_guidance="current B")
    with session_scope(current) as db:
        from meme_arsenal.models import Classification
        job = db.get(RemixJobRecord, job_id)
        db.add(Classification(media_item_id=job.left_id, model="synthetic-current",
                              prompt_version="fixture", summary="Fresh source metadata after enqueue"))
    calls = []
    def request(selected, model, operation, payload, **kwargs):
        calls.append(payload)
        assert selected.ai_provider == "mock"
        assert selected.google_api_key == "current-dummy"
        assert model == "models/retained-image"
        assert payload["generationConfig"]["imageConfig"]["aspectRatio"] == "1:1"
        if len(calls) == 1:
            assert "Fresh source metadata after enqueue" in payload["contents"][0]["parts"][0]["text"]
        assert "retained A" in payload["contents"][0]["parts"][0]["text"]
        assert "current B" not in payload["contents"][0]["parts"][0]["text"]
        if len(calls) == 1:
            return {"candidates": [{"content": {"parts": [{"text": "No image"}]}}]}
        return _gemini_image_response()
    monkeypatch.setattr("meme_arsenal.remix.post_google_model", request)
    monkeypatch.setattr(worker, "_schedule_rejection_explanation", lambda *a: None)
    assert worker.process_remix_job(current, job_id)["status"] == "ready"
    assert len(calls) == 2
    from meme_arsenal.online import _ready_remix_result_or_409, _job_payload
    from meme_arsenal.remix_execution import ordered_text_digest
    with session_scope(current) as db:
        job = db.get(RemixJobRecord, job_id)
        evidence = job.result_json["result"]["generation_provenance"]
        assert evidence["capture"] == capture.payload()
        assert evidence["attempt_number"] == 2
        assert evidence["mitigation_id"] != "original"
        assert evidence["metadata_origin"] == "model_json"
        assert evidence["submitted_text_digest"] == ordered_text_digest([
            p["text"] for p in calls[-1]["contents"][0]["parts"] if "text" in p
        ])
        assert "current-dummy" not in str(evidence)
        assert _job_payload(job)["result"]["generation_provenance"] == evidence
        historical = dict(evidence)
        historical["capture"] = {**evidence["capture"], "policy_digest": "0" * 64}
        job.result_json = {"result": {**job.result_json["result"], "generation_provenance": historical}}
        restored = _ready_remix_result_or_409(current, job)
        assert restored.generation_provenance.payload() == historical
        original_bytes = restored.image_path.read_bytes()
        for raw in (None, {}, {**historical, "attempt_number": False}):
            job.result_json = {"result": {**job.result_json["result"], "generation_provenance": raw}}
            if raw is None:
                assert _ready_remix_result_or_409(current, job).generation_provenance is None
            else:
                from fastapi import HTTPException
                with pytest.raises(HTTPException) as error:
                    _ready_remix_result_or_409(current, job)
                assert error.value.status_code == 409
            assert restored.image_path.read_bytes() == original_bytes
            assert job.status == "ready"


@pytest.mark.parametrize("explicit", [True, False])
def test_online_creation_captures_server_identity(settings, fixture_dir, monkeypatch, explicit):
    from fastapi.testclient import TestClient
    from meme_arsenal.online import create_app
    original = replace(settings, ai_provider="mock" if explicit else "google",
                       remix_provider="google" if explicit else None,
                       classification_provider="mock", google_api_key="dummy-old",
                       remix_model="enqueue-image", public_session_cookie_secure=False)
    client = TestClient(create_app(original))
    ids = []
    for name in ("deploy-fails.png", "smug-reaction.png"):
        response = client.post("/api/remix/uploads", files={"file": (name, (fixture_dir / name).read_bytes(), "image/png")})
        assert response.status_code == 200
        ids.append(response.json()["id"])
    response = client.post("/api/remix/jobs", json={"left_id": ids[0], "right_id": ids[1],
        "execution_capture_json": {"model": "client-forgery"},
        "tweaks": {"no_text": True, "analysis_prompt_version": "old-analysis"}})
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    current = replace(original, ai_provider="mock", remix_provider=None,
                      remix_model="changed", google_api_key="dummy-current")
    def request(selected, model, operation, payload):
        assert selected.google_api_key == "dummy-current"
        assert model == "models/enqueue-image"
        return _gemini_image_response()
    monkeypatch.setattr("meme_arsenal.remix.post_google_model", request)
    assert worker.process_remix_job(current, job_id)["status"] == "ready"
    with session_scope(current) as db:
        job = db.get(RemixJobRecord, job_id)
        assert job.execution_first_captured_attempt == 1
        assert job.tweaks_json["analysis_prompt_version"] == "old-analysis"


def test_local_capture_and_tweaks_survive_mutation(settings, fixture_dir, monkeypatch):
    from meme_arsenal.remix_server import RemixJobStore
    from meme_arsenal.remix import RemixTweaks
    original = replace(settings, remix_provider="google", google_api_key="dummy", remix_model="local-image",
                       remix_style_guidance="retained local A")
    _prepare_archive(original, fixture_dir)
    with session_scope(original) as db:
        ids = list(db.scalars(select(MediaItem.id)))
    store = RemixJobStore(original)
    monkeypatch.setattr(store._executor, "submit", lambda *args: None)
    tweaks = RemixTweaks(no_text=True, analysis_prompt_version="old-local")
    try:
        job = store.create_job(*ids, tweaks=tweaks)
        tweaks.no_text = False
        store.settings = replace(original, remix_provider=None, remix_model="changed", google_api_key="current",
                                 remix_style_guidance="current local B")
        calls = []
        def request(selected, model, operation, payload):
            calls.append(payload)
            assert model == "models/local-image"
            assert selected.google_api_key == "current"
            assert "retained local A" in payload["contents"][0]["parts"][0]["text"]
            assert "current local B" not in payload["contents"][0]["parts"][0]["text"]
            if len(calls) == 1:
                return {"candidates": [{"content": {"parts": [{"text": "No image"}]}}]}
            return _gemini_image_response()
        monkeypatch.setattr("meme_arsenal.remix.post_google_model", request)
        store._run_job(job.id)
        assert job.status == "ready"
        assert job.tweaks.no_text
        assert job.execution_capture.origin == "new_job"
        result = job.result
        evidence = result.generation_provenance
        assert evidence.capture == job.execution_capture
        assert evidence.attempt_number == 2
        assert len(calls) == 2
        def fail_save(*args, **kwargs):
            assert args[2] is result
            assert args[2].generation_provenance is evidence
            raise RuntimeError("retryable fixture failure")
        monkeypatch.setattr("meme_arsenal.remix_server.save_remix_result", fail_save)
        store._save_job(job.id, result)
        assert job.status == "ready"
        assert job.result is result
        assert job.result.generation_provenance is evidence
        assert result.image_path.exists()
    finally:
        store.shutdown()


def test_legacy_claim_pg_serializes_different_candidates(postgres_settings, fixture_dir):
    """CI-only disposable PG proof: second claimant really waits on the row lock."""
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from meme_arsenal.db import _session_factory
    settings = replace(postgres_settings, remix_provider="google", remix_model="winner")
    job_id = queued(settings, fixture_dir)
    with session_scope(settings) as db:
        db.add(RemixAttemptRecord(job_id=job_id, attempt=4, mitigation_id="original",
                                 label="Old", description="Old", status="interrupted", message="Old"))
    first = worker.owned_job_execution(settings, job_id)
    other = replace(settings, remix_model="loser")
    second = worker.owned_job_execution(other, job_id)
    entered, release = Event(), Event()
    # Hold the actual winner's transaction after SELECT FOR UPDATE until the loser starts.
    engine = _session_factory(settings).kw["bind"]
    def limits(conn):
        conn.exec_driver_sql("SET LOCAL lock_timeout = '250ms'")
        conn.exec_driver_sql("SET LOCAL statement_timeout = '10s'")
    event.listen(engine, "begin", limits)
    winner_locked, loser_started = Event(), Event()
    def locked(conn, cursor, statement, parameters, context, many):
        if "FOR UPDATE" in statement and not winner_locked.is_set():
            winner_locked.set()
            assert release.wait(4)
        elif "FOR UPDATE" in statement:
            entered.set()
    def starting(conn, cursor, statement, parameters, context, many):
        if "FOR UPDATE" in statement and winner_locked.is_set():
            loser_started.set()
    event.listen(engine, "after_cursor_execute", locked)
    event.listen(engine, "before_cursor_execute", starting)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            winner = pool.submit(worker._claim_remix_job, settings, job_id, first)
            try:
                assert winner_locked.wait(2)
                future = pool.submit(worker._claim_remix_job, other, job_id, second)
                assert loser_started.wait(2)
                # A server lock timeout proves the losing SQL request was actually blocked.
                with pytest.raises(OperationalError) as blocked:
                    future.result(timeout=2)
                assert blocked.value.orig.sqlstate == "55P03"
                assert not entered.is_set()
                release.set()
                assert winner.result(timeout=5) == "claimed"
                assert worker._claim_remix_job(other, job_id, second) == "preparing_media"
            finally:
                release.set()
        with session_scope(settings) as db:
            job = db.get(RemixJobRecord, job_id)
            assert job.execution_capture_json == first.capture.payload()
            assert job.execution_first_captured_attempt == 5
            assert [a.attempt for a in job.attempts] == [4]
    finally:
        event.remove(engine, "begin", limits)
        event.remove(engine, "after_cursor_execute", locked)
        event.remove(engine, "before_cursor_execute", starting)
