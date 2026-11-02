from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
import uuid
import pytest
from typing import Any
from dataclasses import replace

from PIL import Image
from sqlalchemy import select

from meme_arsenal.ai_circuit import open_ai_credit_circuit
from meme_arsenal.db import session_scope
from meme_arsenal.google_api import GeminiApiError, GeminiRateLimitError
from meme_arsenal.media import process_pending_media
from meme_arsenal.models import MediaItem, RemixAttemptRecord, RemixJobRecord
from meme_arsenal.online_worker import (
    _claim_remix_job,
    _recover_stale_jobs,
    _suggested_retry as worker_suggested_retry,
    process_remix_job,
    run_worker,
)
from meme_arsenal.telegram_importer import import_fixture_messages


@pytest.fixture()
def settings(settings):
    # Generation is mocked or transported through explicit test stubs in this module.
    return replace(settings, remix_provider="google", google_api_key="synthetic-worker-key")


@pytest.mark.parametrize('selected_by', ['redis', 'database'])
def test_worker_does_not_claim_after_stop_during_selection(monkeypatch, selected_by):
    from threading import Event
    from meme_arsenal import online_worker as worker
    stopped = Event()
    calls = []
    monkeypatch.setattr(worker, 'require_owner', lambda _: None)
    monkeypatch.setattr(worker, '_recover_stale_jobs', lambda _: None)
    monkeypatch.setattr(worker, '_ai_work_paused', lambda _: False)
    def select(_):
        stopped.set()
        return 'still-queued-in-database'
    monkeypatch.setattr(worker, '_pop_redis_job', select if selected_by == 'redis' else lambda _: None)
    monkeypatch.setattr(worker, '_next_queued_job', select)
    monkeypatch.setattr(worker, 'process_remix_job', lambda *args: calls.append(args))
    worker.run_worker(None, stop=stopped, once=True)
    assert calls == []


def test_online_worker_rate_limit_retry_suggestion_includes_cooldown():
    explicit = GeminiRateLimitError("quota exhausted", retry_after_seconds=60)
    fallback = GeminiRateLimitError("quota exhausted")

    assert worker_suggested_retry(explicit, exhausted=False) == "Wait 1m, then retry the same pair."
    assert worker_suggested_retry(fallback, exhausted=False) == "Wait 1m, then retry the same pair."


@pytest.mark.parametrize("status_code", [400, 401, 403, 500])
def test_online_worker_does_not_repeat_provider_errors_as_content_retries(monkeypatch, settings, fixture_dir, status_code):
    _prepare_archive(settings, fixture_dir)
    with session_scope(settings) as db:
        ids = list(db.scalars(select(MediaItem.id)))
    job_id = _create_worker_job(settings, *ids)
    calls = []

    def rejected_request(*args, **kwargs):
        calls.append(True)
        raise GeminiApiError("API key not valid" if status_code != 500 else "Provider unavailable", status_code=status_code)

    monkeypatch.setattr("meme_arsenal.online_worker.generate_remix", rejected_request)
    assert process_remix_job(settings, job_id)["status"] == "failed"
    assert calls == [True]
    with session_scope(settings) as db:
        job = db.get(RemixJobRecord, job_id)
        assert len(job.attempts) == 1
        if status_code != 500:
            assert "credentials" in job.result_json["failure_explanation"]
            assert "API key" in job.result_json["suggested_retry"]


@pytest.mark.parametrize("denial", [False, True])
def test_online_worker_credit_depletion_keeps_job_queued(monkeypatch, settings, fixture_dir, denial):
    _prepare_archive(settings, fixture_dir)
    live_settings = replace(settings, ai_provider="google", google_api_key="test-google-key")
    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        job_id = _create_worker_job(live_settings, media_ids[0], media_ids[1])

    def fake_generate_remix(*_args, **_kwargs):
        if denial:
            from meme_arsenal.google_api import GeminiCreditCircuitOpenError
            raise GeminiCreditCircuitOpenError("state unavailable", status="CREDIT_CIRCUIT_UNAVAILABLE")
        raise GeminiRateLimitError(
            "Gemini API generateContent failed with HTTP 429: "
            "Your prepayment credits are depleted. Please go to AI Studio to increase your limit.",
            status_code=429,
            status="RESOURCE_EXHAUSTED",
        )

    monkeypatch.setattr("meme_arsenal.online_worker.generate_remix", fake_generate_remix)

    output = process_remix_job(live_settings, job_id)
    assert output == {"status": "queued", "paused": True}

    with session_scope(live_settings) as session:
        job = session.get(RemixJobRecord, job_id)
        assert job is not None
        assert job.status == "queued"
        assert job.message == ("Waiting for Gemini credit recovery" if denial else "Waiting for Gemini credits")
        if not denial:
            assert job.retry_after_seconds is not None
        assert ("unavailable circuit state" if denial else "prepaid credits are depleted") in job.result_json["failure_explanation"]
        assert "worker-only operators should run ai-resume" in (job.result_json.get("suggested_retry") or "")
        attempts = sorted(job.attempts, key=lambda attempt: attempt.attempt)
        assert attempts[-1].status == "paused"
        assert attempts[-1].message == job.message

    if denial:
        open_ai_credit_circuit(live_settings)
    second_output = process_remix_job(live_settings, job_id)
    assert second_output == {"status": "queued", "paused": True}
    with session_scope(live_settings) as session:
        job = session.get(RemixJobRecord, job_id)
        assert job is not None
        assert [attempt.attempt for attempt in sorted(job.attempts, key=lambda row: row.attempt)] == [1]


def test_online_worker_claims_a_queued_job_only_once(settings, fixture_dir):
    _prepare_archive(settings, fixture_dir)
    with session_scope(settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        job_id = _create_worker_job(settings, media_ids[0], media_ids[1])

    assert _claim_remix_job(settings, job_id) == "claimed"
    assert _claim_remix_job(settings, job_id) == "preparing_media"


def test_online_worker_recovers_stale_jobs_and_interrupts_attempts(settings, fixture_dir):
    _prepare_archive(settings, fixture_dir)
    stale_at = datetime.now(timezone.utc) - timedelta(hours=2)
    with session_scope(settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        job_id = _create_worker_job(settings, media_ids[0], media_ids[1])
        job = session.get(RemixJobRecord, job_id)
        assert job is not None
        job.status = "calling_gemini"
        job.updated_at = stale_at
        session.add(
            RemixAttemptRecord(
                job_id=job_id,
                attempt=1,
                mitigation_id="original",
                status="running",
                message="Calling Gemini",
            )
        )

    assert _recover_stale_jobs(settings) == 1
    with session_scope(settings) as session:
        job = session.get(RemixJobRecord, job_id)
        assert job is not None
        assert job.status == "queued"
        assert job.message == "Recovered after an interrupted worker"
        assert job.attempts[0].status == "interrupted"


def test_online_worker_selects_identity_before_credit_gate(monkeypatch, settings):
    live_settings = replace(settings, ai_provider="google", google_api_key="test-google-key")
    open_ai_credit_circuit(live_settings)
    pops: list[bool] = []

    monkeypatch.setattr(
        "meme_arsenal.online_worker._pop_redis_job",
        lambda _settings: pops.append(True),
    )

    run_worker(live_settings, once=True)
    assert pops == [True]


def _prepare_archive(settings, fixture_dir):
    with session_scope(settings) as session:
        import_fixture_messages(session, settings, fixture_dir)
        process_pending_media(session, settings)


def _create_worker_job(settings, left_id: int, right_id: int, *, safety_mode: str = "standard") -> str:
    with session_scope(settings) as session:
        job = RemixJobRecord(
            id=uuid.uuid4().hex,
            session_id=settings.owner_session_id,
            left_id=left_id,
            right_id=right_id,
            safety_mode=safety_mode,
            status="queued",
            message="Queued",
            tweaks_json={},
            result_json={},
        )
        session.add(job)
        session.flush()
        return job.id


def _png_data() -> bytes:
    image = Image.new("RGB", (96, 96), color=(42, 120, 200))
    from io import BytesIO

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _gemini_image_response(summary: str = "Generated remix") -> dict:
    metadata = {
        "summary": summary,
        "visible_text": "from online worker",
        "narration": "online worker remix generation",
        "language": "en",
        "meme_format": "generated meme",
        "tone": ["playful"],
        "reaction_type": ["surprise"],
        "topics": ["worker", "remix"],
        "keywords": ["worker", "remix"],
        "people_or_characters": [],
        "safety_flags": [],
        "search_phrases": ["worker test"],
        "confidence": 0.9,
    }
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": json.dumps(metadata)},
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": base64.b64encode(_png_data()).decode("ascii"),
                            }
                        },
                    ]
                }
            }
        ]
    }


def _gemini_blocked_response(message: str = "Image safety policy block.") -> dict:
    return {
        "promptFeedback": {
            "blockReason": "IMAGE_SAFETY",
            "blockReasonMessage": message,
            "safetyRatings": [
                {
                    "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "probability": "HIGH",
                    "blocked": True,
                }
            ],
        },
        "candidates": [{"finishReason": "IMAGE_SAFETY", "content": {"parts": []}}],
    }


def test_online_worker_retries_with_failure_context(monkeypatch, settings, fixture_dir):
    _prepare_archive(settings, fixture_dir)
    live_settings = replace(settings, ai_provider="google", google_api_key="test-google-key")
    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        job_id = _create_worker_job(live_settings, media_ids[0], media_ids[1])

    payloads: list[dict[str, Any]] = []
    responses = [_gemini_blocked_response(), _gemini_image_response("Worker retry output")]

    def fake_post(_settings, _model, _method, payload):
        payloads.append(payload)
        return responses.pop(0)

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)

    output = process_remix_job(live_settings, job_id)
    assert output["status"] == "ready"

    assert len(payloads) == 2
    second_text = " ".join(
        part.get("text", "") for part in payloads[1].get("contents", [{}])[0].get("parts", []) if "text" in part
    )
    assert "Retry context" in second_text
    assert "IMAGE_SAFETY" in second_text or "Image safety policy block." in second_text
    assert "The previous attempt was blocked" in second_text

    with session_scope(live_settings) as session:
        job = session.get(RemixJobRecord, job_id)
        assert job is not None
        attempts = sorted(job.attempts, key=lambda attempt: attempt.attempt)
        assert [attempt.status for attempt in attempts] == ["failed", "succeeded"]
        assert attempts[0].mitigation_id == "original"
        assert attempts[0].block_reason == "IMAGE_SAFETY"
        assert attempts[0].rejection_reason == "Image Safety"
        assert job.rejection_reason == "Image Safety"
        assert job.result_json["failure_explanation"] is None
        assert job.status == "ready"
        assert job.saved_media_item_id is None
        assert "result" in job.result_json

        evidence = job.result_json["result"]["generation_provenance"]
        assert evidence["attempt_number"] == 2
        assert evidence["mitigation_id"] == attempts[1].mitigation_id
        first_attempt_id = attempts[0].id
    from meme_arsenal import online_worker
    monkeypatch.setattr(online_worker, "explain_remix_rejection", lambda *_: "Late explanation")
    online_worker._persist_rejection_explanation(live_settings, job_id, first_attempt_id, {})
    with session_scope(live_settings) as session:
        job = session.get(RemixJobRecord, job_id)
        assert job.rejection_reason == "Late explanation"
        assert job.result_json["result"]["generation_provenance"] == evidence


def test_online_worker_explains_no_image_attempt(monkeypatch, settings, fixture_dir):
    _prepare_archive(settings, fixture_dir)
    live_settings = replace(settings, ai_provider="google", google_api_key="test-google-key")
    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        job_id = _create_worker_job(live_settings, media_ids[0], media_ids[1])

    payloads: list[dict[str, Any]] = []
    responses = [
        {"candidates": [{"content": {"parts": [{"text": "Only text came back from this attempt."}]}}]},
        _gemini_image_response("Worker no-image retry output"),
    ]

    def fake_post(_settings, _model, _method, payload):
        payloads.append(payload)
        return responses.pop(0)

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)

    output = process_remix_job(live_settings, job_id)
    assert output["status"] == "ready"
    assert len(payloads) == 2

    second_text = " ".join(
        part.get("text", "") for part in payloads[1].get("contents", [{}])[0].get("parts", []) if "text" in part
    )
    assert "Retry context" in second_text
    assert "no generated image" in second_text

    with session_scope(live_settings) as session:
        job = session.get(RemixJobRecord, job_id)
        assert job is not None
        attempts = sorted(job.attempts, key=lambda attempt: attempt.attempt)
        assert [attempt.status for attempt in attempts] == ["failed", "succeeded"]
        assert attempts[0].message == "No image returned"
        assert attempts[0].rejection_reason == "Text-only response"
        assert job.rejection_reason == "Text-only response"
        assert job.result_json["failure_explanation"] is None
        assert job.status == "ready"
        assert job.saved_media_item_id is None
        assert "result" in job.result_json


def test_online_worker_blocks_with_failure_explanation_when_exhausted(monkeypatch, settings, fixture_dir):
    _prepare_archive(settings, fixture_dir)
    live_settings = replace(settings, ai_provider="google", google_api_key="test-google-key")
    with session_scope(live_settings) as session:
        media_ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
        job_id = _create_worker_job(live_settings, media_ids[0], media_ids[1])

    def fake_post(_settings, _model, _method, payload):
        return _gemini_blocked_response("Persistent block on all retries.")

    monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_post)

    output = process_remix_job(live_settings, job_id)
    assert output["status"] == "blocked"

    with session_scope(live_settings) as session:
        job = session.get(RemixJobRecord, job_id)
        assert job is not None
        assert job.status == "blocked"
        assert job.result_json.get("failure_explanation")
        explanation = job.result_json.get("failure_explanation") or ""
        assert "retry" in explanation.lower()
        assert job.result_json.get("suggested_retry")
        assert job.block_reason == "IMAGE_SAFETY"
        assert job.rejection_reason == "Image Safety"
        attempts = sorted(job.attempts, key=lambda attempt: attempt.attempt)
        assert attempts[0].block_reason == "IMAGE_SAFETY"
        assert attempts[0].rejection_reason == "Image Safety"
        assert [attempt.status for attempt in sorted(job.attempts, key=lambda attempt: attempt.attempt)] == [
            "failed",
            "failed",
            "failed",
            "failed",
            "failed",
        ]
