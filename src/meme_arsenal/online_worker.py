from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import time
from threading import Event
from typing import Any
from dataclasses import dataclass
from copy import deepcopy

from sqlalchemy import func, select, update

from .ai_circuit import ai_circuit_status, open_ai_credit_circuit
from .config import Settings
from .db import session_scope
from .owner import require_owner, source_eligible
from .google_api import (GeminiCreditCircuitOpenError, GeminiRateLimitError,
                         google_usage_context, google_work_paused, is_credit_depletion_error)
from .models import MediaItem, RemixAttemptRecord, RemixJobRecord
from .remix_execution import RemixExecutionCapture, capture_execution
from .remix_failures import (
    _attempt_message,
    _failure_explanation,
    _rate_limit_retry_after_seconds,
    _suggested_retry,
    can_retry_with_mitigation,
)
from .remix import (
    RemixBlockedError,
    RemixNoImageError,
    RemixResult,
    build_remix_mitigation_plan,
    explain_remix_rejection,
    generate_remix,
    local_remix_rejection_reason,
    remix_tweaks_from_payload,
)


_REJECTION_EXPLANATION_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="online-remix-explain")




def run_worker(settings: Settings, *, poll_seconds: float = 3.0, once: bool = False,
               stop: Event | None = None) -> None:
    require_owner(settings)
    stop = stop or Event()
    next_recovery_at = 0.0
    while not stop.is_set():
        now = time.monotonic()
        if now >= next_recovery_at:
            _recover_stale_jobs(settings)
            next_recovery_at = now + 30.0
        if stop.is_set():
            return
        job_id = _pop_redis_job(settings) or _next_queued_job(settings)
        if stop.is_set():
            # A popped hint is disposable; the unclaimed SQL job remains queued.
            return
        if job_id:
            result = process_remix_job(settings, job_id, stop=stop)
            if result and result.get("status") == "missing":
                fallback = _next_queued_job(settings)
                if fallback and not stop.is_set():
                    result = process_remix_job(settings, fallback, stop=stop)
            if result and result.get("paused") and not once:
                stop.wait(max(poll_seconds, 30.0))
            if once:
                return
        elif once:
            return
        else:
            stop.wait(poll_seconds)


def process_remix_job(settings: Settings, job_id: str, *, stop: Event | None = None) -> dict[str, Any]:
    require_owner(settings)
    identity = owned_job_execution(settings, job_id)
    if stop is not None and stop.is_set():
        return {"status": "queued"}
    if identity.status == "invalid":
        return {"status": _invalidate_queued_job(settings, job_id, identity)}
    if identity.status != "queued":
        return {"status": identity.status}
    if _ai_work_paused(identity.capture.apply(settings)):
        return {"status": "queued", "paused": True}
    if stop is not None and stop.is_set():
        return {"status": "queued"}
    claimed_status = _claim_remix_job(settings, job_id, identity, stop=stop)
    if claimed_status != "claimed":
        return {"status": claimed_status}

    plan: list[object] = []
    with session_scope(settings) as session:
        job = session.get(RemixJobRecord, job_id)
        if job is None:
            return {"status": "missing"}
        try:
            _validate_inputs(session, settings, job)
            plan = build_remix_mitigation_plan(
                session,
                identity.capture.apply(settings),
                job.left_id,
                job.right_id,
                safety_mode=job.safety_mode,
            )
        except Exception as exc:
            return _record_failure(settings, job_id, None, "failed", "Remix failed", exc, exhausted=True)

    with session_scope(settings) as session:
        last_attempt_number = session.scalar(
            select(func.max(RemixAttemptRecord.attempt)).where(RemixAttemptRecord.job_id == job_id)
        )
    first_attempt_number = int(last_attempt_number or 0) + 1

    attempt_id: int | None = None
    last_blocking_context: str | None = None
    last_error: Exception | None = None
    for index, mitigation in enumerate(plan):
        label = mitigation.label if mitigation else "Original request"
        description = (
            mitigation.description if mitigation else "Using source visuals and safely filtered metadata."
        )
        attempt_id = None
        with session_scope(settings) as session:
            job = session.get(RemixJobRecord, job_id)
            if job is None:
                return {"status": "missing"}
            attempt = RemixAttemptRecord(
                job_id=job.id,
                attempt=first_attempt_number + index,
                mitigation_id=mitigation.id if mitigation else "original",
                label=label,
                description=description,
                status="running",
                message="Calling Gemini",
                created_at=_utc_now(),
            )
            session.add(attempt)
            job.status = "calling_gemini"
            job.message = "Calling Gemini" if mitigation is None else mitigation.description
            job.updated_at = _utc_now()
            session.flush()
            attempt_id = attempt.id

        result: RemixResult | None = None
        tweaks = None
        with session_scope(settings) as session:
            job = session.get(RemixJobRecord, job_id)
            if job is not None:
                tweaks = remix_tweaks_from_payload(job.tweaks_json or None)

        try:
            with session_scope(settings) as session, google_usage_context(job_id=job_id, operation="remix"):
                job = session.get(RemixJobRecord, job_id)
                if job is None:
                    return {"status": "missing"}
                _validate_inputs(session, settings, job)
                result = generate_remix(
                    session,
                    settings,
                    job.left_id,
                    job.right_id,
                    job_id=job.id,
                    tweaks=tweaks,
                    mitigation=mitigation,
                    failure_context=last_blocking_context,
                    safety_mode=job.safety_mode,
                    execution_capture=identity.capture,
                    attempt_number=first_attempt_number + index,
                )

            with session_scope(settings) as session:
                job = session.get(RemixJobRecord, job_id)
                if job is None:
                    return {"status": "missing"}
                if result is None:
                    raise RuntimeError("No generated remix was returned")
                job.result_json = {
                    **(job.result_json or {}),
                    "result": result.payload(),
                    "failure_explanation": None,
                    "suggested_retry": None,
                }
                _update_attempt(session, attempt_id, status="succeeded", message="Generated image")
                job.status = "ready"
                job.message = "Ready"
                job.updated_at = _utc_now()
                return {"status": "ready", "job_id": job.id}
        except GeminiRateLimitError as exc:
            if is_credit_depletion_error(exc):
                return _record_credit_pause(settings, job_id, attempt_id, exc)
            return _record_failure(settings, job_id, attempt_id, "rate_limited", "Gemini rate limit reached", exc)
        except RemixBlockedError as exc:
            last_error = exc
            last_blocking_context = exc.failure_explanation or (
                f"Previous attempt was blocked: {exc.block_reason or 'blocked'}"
            )
            rejection_reason = local_remix_rejection_reason(exc.rejection_context)
            with session_scope(settings) as session:
                job = session.get(RemixJobRecord, job_id)
                if job is not None:
                    job.block_reason = exc.block_reason
                    job.rejection_reason = job.rejection_reason or rejection_reason
                    job.updated_at = _utc_now()
                _update_attempt(
                    session,
                    attempt_id,
                    status="failed",
                    message="Safety filtered",
                    error=str(exc),
                    block_reason=exc.block_reason,
                    rejection_reason=rejection_reason,
                )
            _schedule_rejection_explanation(settings, job_id, attempt_id, exc.rejection_context)
            if index + 1 < len(plan):
                continue
            return _record_failure(
                settings,
                job_id,
                attempt_id,
                "blocked",
                "Remix safety filtered",
                exc,
                exhausted=True,
            )
        except Exception as exc:  # noqa: BLE001 - worker must persist errors into the job row.
            last_error = exc
            rejection_reason = (
                local_remix_rejection_reason(exc.rejection_context)
                if isinstance(exc, RemixNoImageError)
                else None
            )
            with session_scope(settings) as session:
                job = session.get(RemixJobRecord, job_id)
                if job is not None and rejection_reason:
                    job.rejection_reason = job.rejection_reason or rejection_reason
                    job.updated_at = _utc_now()
                _update_attempt(
                    session,
                    attempt_id,
                    status="failed",
                    message=_attempt_message(exc),
                    error=str(exc),
                    rejection_reason=rejection_reason,
                )
            if isinstance(exc, RemixNoImageError):
                last_blocking_context = "Previous attempt returned text or metadata but no generated image."
                _schedule_rejection_explanation(settings, job_id, attempt_id, exc.rejection_context)
            if can_retry_with_mitigation(exc) and index + 1 < len(plan):
                continue
            return _record_failure(settings, job_id, attempt_id, "failed", "Remix failed", exc, exhausted=True)

    if last_error is not None:
        return _record_failure(
            settings,
            job_id,
            attempt_id,
            "failed",
            "Remix failed",
            last_error,
            exhausted=True,
        )

    return _record_failure(
        settings,
        job_id,
        attempt_id,
        "failed",
        "Remix failed",
        RuntimeError("No mitigation attempts ran"),
        exhausted=True,
    )


def _validate_inputs(session, settings, job):
    if job.session_id != settings.owner_session_id or not all(
        source_eligible(session, settings, session.get(MediaItem, id), settings.owner_session_id)
        for id in (job.left_id, job.right_id)
    ):
        raise ValueError("Source unavailable to configured owner")


@dataclass(frozen=True)
class OwnedJobExecution:
    status: str
    capture: RemixExecutionCapture | None = None
    stored_capture: Any = None
    boundary: int | None = None


def owned_job_execution(settings: Settings, job_id: str) -> OwnedJobExecution:
    """Read-only identity seam for dispatch; foreign jobs are indistinguishable from missing."""
    require_owner(settings)
    with session_scope(settings) as session:
        job = session.scalar(select(RemixJobRecord).where(
            RemixJobRecord.id == job_id, RemixJobRecord.session_id == settings.owner_session_id))
        if job is None:
            return OwnedJobExecution("missing")
        if job.status != "queued":
            return OwnedJobExecution(job.status)
        raw, boundary = deepcopy(job.execution_capture_json), job.execution_first_captured_attempt
        try:
            capture = RemixExecutionCapture.parse(raw)
            last = session.scalar(select(func.max(RemixAttemptRecord.attempt)).where(
                RemixAttemptRecord.job_id == job_id)) or 0
            if capture is None:
                if boundary is not None:
                    raise ValueError("boundary")
                capture = capture_execution(settings, "legacy_first_execution")
            elif (type(boundary) is not int or boundary < 1 or boundary > last + 1
                  or (capture.origin != "legacy_first_execution" and boundary != 1)):
                raise ValueError("boundary")
            capture.require_current_policy()
        except (ValueError, TypeError):
            return OwnedJobExecution("invalid", stored_capture=raw, boundary=boundary)
        return OwnedJobExecution("queued", capture, raw, boundary)


def _locked_candidate(session, settings, job_id, identity):
    job = session.scalar(select(RemixJobRecord).where(
        RemixJobRecord.id == job_id, RemixJobRecord.session_id == settings.owner_session_id
    ).with_for_update())
    if job is None:
        return None, "missing"
    if job.status != "queued":
        return None, job.status
    if (job.execution_capture_json != identity.stored_capture
            or job.execution_first_captured_attempt != identity.boundary):
        return None, "queued"
    return job, "queued"


def _invalidate_queued_job(settings, job_id, identity):
    with session_scope(settings) as session:
        job, status = _locked_candidate(session, settings, job_id, identity)
        if job is None:
            return status
        result = session.execute(update(RemixJobRecord).where(
            RemixJobRecord.id == job_id, RemixJobRecord.session_id == settings.owner_session_id,
            RemixJobRecord.status == "queued").values(status="failed",
                message="Remix configuration is incompatible. Create a new job.",
                error="Remix configuration is incompatible. Create a new job.", updated_at=_utc_now()))
        return "failed" if result.rowcount == 1 else "queued"


def _claim_remix_job(settings: Settings, job_id: str, identity=None, *, stop=None) -> str:
    identity = identity or owned_job_execution(settings, job_id)
    if identity.status != "queued":
        return identity.status
    now = _utc_now()
    with session_scope(settings) as session:
        job, status = _locked_candidate(session, settings, job_id, identity)
        if job is None:
            return status
        identity.capture.require_current_policy()
        if stop is not None and stop.is_set():
            return "queued"
        values = {}
        if job.execution_capture_json is None:
            last = session.scalar(select(func.max(RemixAttemptRecord.attempt)).where(
                RemixAttemptRecord.job_id == job_id)) or 0
            values = dict(execution_capture_json=identity.capture.payload(),
                          execution_first_captured_attempt=int(last) + 1)
        if stop is not None and stop.is_set():
            return "queued"
        result = session.execute(
            update(RemixJobRecord)
            .where(RemixJobRecord.id == job_id, RemixJobRecord.session_id == settings.owner_session_id, RemixJobRecord.status == "queued")
            .values(
                status="preparing_media",
                message="Preparing source memes",
                error=None,
                retry_after_seconds=None,
                updated_at=now,
                **values,
            )
        )
        if result.rowcount == 1:
            return "claimed"
        status = session.scalar(select(RemixJobRecord.status).where(RemixJobRecord.id == job_id, RemixJobRecord.session_id == settings.owner_session_id))
        return str(status) if status else "missing"


def _recover_stale_jobs(settings: Settings) -> int:
    cutoff = _utc_now() - timedelta(seconds=max(60, settings.remix_job_stale_seconds))
    with session_scope(settings) as session:
        stale_ids = list(
            session.scalars(
                select(RemixJobRecord.id).where(
                    RemixJobRecord.session_id == settings.owner_session_id,
                    RemixJobRecord.status.in_(("preparing_media", "calling_gemini")),
                    RemixJobRecord.updated_at < cutoff,
                ).with_for_update(skip_locked=True)
            )
        )
        if not stale_ids:
            return 0
        session.execute(
            update(RemixAttemptRecord)
            .where(
                RemixAttemptRecord.job_id.in_(stale_ids),
                RemixAttemptRecord.status == "running",
            )
            .values(
                status="interrupted",
                message="Worker stopped before this attempt completed",
            )
        )
        session.execute(
            update(RemixJobRecord)
            .where(RemixJobRecord.id.in_(stale_ids))
            .values(
                status="queued",
                message="Recovered after an interrupted worker",
                updated_at=_utc_now(),
            )
        )
        return len(stale_ids)




def _next_queued_job(settings: Settings) -> str | None:
    with session_scope(settings) as session:
        job = session.scalar(
            select(RemixJobRecord)
            .where(RemixJobRecord.status == "queued", RemixJobRecord.session_id == settings.owner_session_id)
            .order_by(RemixJobRecord.created_at)
            .limit(1)
        )
        return job.id if job else None


def _pop_redis_job(settings: Settings) -> str | None:
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        value = client.rpop("meme_arsenal:remix_jobs")
        return str(value) if value else None
    except Exception:
        return None


def _ai_work_paused(settings: Settings) -> bool:
    if settings.effective_remix_provider != "google":
        return False
    return google_work_paused(settings)


def _record_credit_pause(
    settings: Settings,
    job_id: str,
    attempt_id: int | None,
    exc: GeminiRateLimitError,
) -> dict[str, Any]:
    denied = isinstance(exc, GeminiCreditCircuitOpenError)
    message = "Waiting for Gemini credit recovery" if denied else "Waiting for Gemini credits"
    try:
        status = ai_circuit_status(settings)
        if not status.is_open and not isinstance(exc, GeminiCreditCircuitOpenError):
            status = open_ai_credit_circuit(settings)
        retry_after_seconds = status.retry_after_seconds()
    except Exception:
        retry_after_seconds = None

    with session_scope(settings) as session:
        job = session.get(RemixJobRecord, job_id)
        if job:
            job.status = "queued"
            job.message = message
            job.error = str(exc)
            job.retry_after_seconds = retry_after_seconds
            payload = dict(job.result_json or {})
            payload["result"] = payload.get("result")
            payload["failure_explanation"] = (
                ("Gemini work is paused by the credit circuit or unavailable circuit state. " if denied else
                 "Gemini work is paused because the API account's prepaid credits are depleted. ") +
                "This remix will remain queued."
            )
            payload["suggested_retry"] = (
                "sync-online schedules credit probes; worker-only operators should run ai-resume. "
                "The worker can resume this retained job after recovery."
            )
            job.result_json = payload
            job.updated_at = _utc_now()
        _update_attempt(
            session,
            attempt_id,
            status="paused",
            message=message,
            error=str(exc),
        )
    return {"status": "queued", "paused": True}


def _record_failure(
    settings: Settings,
    job_id: str,
    attempt_id: int | None,
    status: str,
    message: str,
    exc: Exception,
    *,
    exhausted: bool = False,
    failure_context: str | None = None,
) -> dict[str, Any]:
    with session_scope(settings) as session:
        job = session.get(RemixJobRecord, job_id)
        if job:
            job.status = status
            job.message = message
            job.error = str(exc)
            if isinstance(exc, GeminiRateLimitError) and not is_credit_depletion_error(exc):
                job.retry_after_seconds = _rate_limit_retry_after_seconds(exc)
            if isinstance(exc, RemixBlockedError):
                job.block_reason = exc.block_reason
                job.rejection_reason = job.rejection_reason or local_remix_rejection_reason(exc.rejection_context)
            if isinstance(exc, RemixNoImageError):
                job.rejection_reason = job.rejection_reason or local_remix_rejection_reason(exc.rejection_context)
            payload = dict(job.result_json or {})
            payload["result"] = payload.get("result")
            payload["failure_explanation"] = failure_context or _failure_explanation(exc)
            payload["suggested_retry"] = _suggested_retry(exc, exhausted=exhausted)
            job.result_json = payload
            job.updated_at = _utc_now()
        _update_attempt(session, attempt_id, status="failed", message=message, error=str(exc))
    return {"status": status, "error": str(exc)}


def _update_attempt(
    session,
    attempt_id: int | None,
    *,
    status: str,
    message: str,
    error: str | None = None,
    block_reason: str | None = None,
    rejection_reason: str | None = None,
) -> None:
    if attempt_id is None:
        return
    attempt = session.get(RemixAttemptRecord, attempt_id)
    if not attempt:
        return
    attempt.status = status
    attempt.message = message
    attempt.error = error
    if block_reason is not None:
        attempt.block_reason = block_reason
    if rejection_reason is not None:
        attempt.rejection_reason = rejection_reason


def _schedule_rejection_explanation(
    settings: Settings,
    job_id: str,
    attempt_id: int | None,
    context: dict[str, Any] | None,
) -> None:
    if attempt_id is None:
        return
    _REJECTION_EXPLANATION_EXECUTOR.submit(_persist_rejection_explanation, settings, job_id, attempt_id, context)


def _persist_rejection_explanation(
    settings: Settings,
    job_id: str,
    attempt_id: int,
    context: dict[str, Any] | None,
) -> None:
    try:
        with google_usage_context(job_id=job_id, operation="rejection_explanation"):
            reason = explain_remix_rejection(settings, context)
    except Exception:
        return
    if not reason:
        return
    with session_scope(settings) as session:
        job = session.get(RemixJobRecord, job_id)
        attempt = session.get(RemixAttemptRecord, attempt_id)
        if attempt:
            attempt.rejection_reason = reason
        if job:
            job.rejection_reason = reason
            job.updated_at = _utc_now()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
