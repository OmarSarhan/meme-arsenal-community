from __future__ import annotations

import json
from copy import deepcopy
from io import BytesIO
import os
import mimetypes
import posixpath
import threading
from pathlib import Path
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse
from python_multipart.multipart import parse_form

from sqlalchemy import func, select

from .branding import APP_NAME, REMIX_SERVER_PRODUCT
from .catalog import catalog_saved_at, export_catalog
from .config import Settings
from .db import session_scope
from .embeddings import embed_media_item
from .media import import_local_media_file, process_media_item, stored_media_file_exists
from .google_api import GeminiRateLimitError, is_credit_depletion_error
from .logging import console
from .ai import classify_media_item, classification_block_reason, classification_status
from .models import Classification, MediaItem, TelegramMessage
from .remix_execution import RemixExecutionCapture, capture_execution
from .result_identity import generation_identity
from .remix_failures import (
    _attempt_message,
    _failure_explanation,
    _rate_limit_retry_after_seconds,
    _suggested_retry,
    can_retry_with_mitigation,
)
from .remix import (
    RemixBlockedError,
    RemixError,
    RemixNoImageError,
    RemixResult,
    RemixTweaks,
    RemixUnavailableError,
    analyze_remix_tweaks,
    build_remix_mitigation_plan,
    explain_remix_rejection,
    generate_remix,
    local_remix_rejection_reason,
    remix_unavailable_reason,
    remix_safety_mode_from_payload,
    remix_tweaks_from_payload,
    save_remix_result,
)


PUBLIC_STATIC_ROOTS = ("/dashboard/dist", "/catalog", "/media")
UPLOAD_CHAT_PREFIX = "dashboard-upload"
DASHBOARD_UPLOAD_ORIGIN = "dashboard_upload"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "base-uri 'none'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'none'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self'; "
        "font-src 'self' data:"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _normalized_url_path(raw_path: str) -> str:
    path = unquote(urlparse(raw_path).path)
    normalized = posixpath.normpath(path)
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if path.endswith("/") and not normalized.endswith("/"):
        normalized = f"{normalized}/"
    return normalized


def _has_hidden_segment(path: str) -> bool:
    return any(segment.startswith(".") for segment in path.split("/") if segment)


def _is_public_static_path(path: str) -> bool:
    if _has_hidden_segment(path):
        return False
    return any(path == root or path.startswith(f"{root}/") for root in PUBLIC_STATIC_ROOTS)


def _is_remix_api_path(path: str) -> bool:
    return path == "/api/remix" or path.startswith("/api/remix/")


def _is_favicon_path(path: str) -> bool:
    return path == "/favicon.ico"


def _draft_upload_chat_id() -> str:
    return UPLOAD_CHAT_PREFIX


def _to_public_media_url(settings: Settings, stored_path: str | None) -> str | None:
    if not stored_path:
        return None
    if not stored_media_file_exists(settings, stored_path):
        return None
    value = str(stored_path).replace("\\", "/")
    return value if value.startswith("/") else f"/{value}"


def _remix_payload(session, settings: Settings, media_item: MediaItem) -> dict[str, Any]:
    message = session.get(TelegramMessage, media_item.telegram_message_id)
    classification = session.scalar(
        select(Classification)
        .where(Classification.media_item_id == media_item.id)
        .order_by(Classification.id.desc())
    )

    return {
        "id": media_item.id,
        "telegram_message_id": message.telegram_message_id if message else 0,
        "chat_id": message.chat_id if message else "",
        "telegram_post_url": message.telegram_post_url if message else None,
        "date": message.date.isoformat() if message and message.date else None,
        "saved_at": catalog_saved_at(media_item, message).isoformat(),
        "added_at": (
            media_item.created_at.replace(tzinfo=timezone.utc)
            if media_item.created_at.tzinfo is None else media_item.created_at
        ).isoformat(),
        "caption": message.caption or "",
        "source_url": None,
        "source_display_url": None,
        "source_site": None,
        "source_title": None,
        "source_description": None,
        "source_embed_url": None,
        "origin": media_item.origin,
        "link_preview": {},
        "grouped_id": message.grouped_id if message else None,
        "forwarded_from": message.forwarded_from if message else None,
        "media_type": media_item.media_type,
        "mime_type": media_item.mime_type,
        "thumbnail_path": _to_public_media_url(settings, media_item.thumbnail_path),
        "preview_path": _to_public_media_url(settings, media_item.preview_path),
        "hover_preview_path": _to_public_media_url(settings, media_item.hover_preview_path),
        "original_path": _to_public_media_url(settings, media_item.original_path)
        if media_item.public_original_allowed or media_item.origin == DASHBOARD_UPLOAD_ORIGIN
        else None,
        "duration_seconds": media_item.duration_seconds,
        "width": media_item.width,
        "height": media_item.height,
        "size_bytes": media_item.size_bytes,
        "sha256": media_item.sha256,
        "perceptual_hash": media_item.perceptual_hash,
        "duplicate_group_id": media_item.duplicate_group_id,
        "processing_status": media_item.processing_status,
        "classification_status": classification_status(classification),
        "classification_block_reason": classification_block_reason(classification),
        "summary": classification.summary if classification else "",
        "visible_text": classification.visible_text if classification else "",
        "narration": classification.narration if classification else "",
        "language": classification.language if classification else None,
        "meme_format": classification.meme_format if classification else None,
        "tone": classification.tone if classification else [],
        "reaction_type": classification.reaction_type if classification else [],
        "topics": classification.topics_json if classification else [],
        "keywords": classification.keywords_json if classification else [],
        "people_or_characters": classification.people_or_characters_json if classification else [],
        "safety_flags": classification.safety_flags_json if classification else [],
        "search_phrases": classification.search_phrases_json if classification else [],
        "favorite": False,
        "archived": False,
        "manual_tags": [],
        "notes": "",
        "server_tags": [],
        "public_status": media_item.public_status,
        "visibility": media_item.visibility,
        "public_original_allowed": media_item.public_original_allowed,
        "remix_sources": [],
        "remix_used_by": [],
    }


def _parse_byte_range(range_header: str, file_size: int) -> tuple[int, int]:
    if file_size <= 0:
        raise ValueError("Cannot serve a range from an empty file")
    unit, _, spec = range_header.partition("=")
    if unit.strip().lower() != "bytes" or not spec or "," in spec:
        raise ValueError("Only a single bytes range is supported")
    start_text, separator, end_text = spec.strip().partition("-")
    if separator != "-":
        raise ValueError("Invalid bytes range")

    if start_text == "":
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise ValueError("Invalid suffix range")
        return max(0, file_size - suffix_length), max(0, file_size - 1)

    start = int(start_text)
    end = int(end_text) if end_text else file_size - 1
    if start < 0 or end < start or start >= file_size:
        raise ValueError("Requested range is not satisfiable")
    return start, min(end, file_size - 1)


@dataclass
class RemixAttempt:
    attempt: int
    mitigation_id: str
    label: str
    description: str
    status: str = "running"
    message: str = ""
    error: str | None = None
    block_reason: str | None = None
    rejection_reason: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "mitigation_id": self.mitigation_id,
            "label": self.label,
            "description": self.description,
            "status": self.status,
            "message": self.message,
            "error": self.error,
            "block_reason": self.block_reason,
            "rejection_reason": self.rejection_reason,
        }


@dataclass
class RemixJob:
    id: str
    left_id: int
    right_id: int
    tweaks: RemixTweaks | None = None
    safety_mode: str = "standard"
    status: str = "queued"
    message: str = "Queued"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    retry_after_seconds: float | None = None
    block_reason: str | None = None
    rejection_reason: str | None = None
    error: str | None = None
    result: RemixResult | None = None
    saved_item: dict[str, Any] | None = None
    embedding_error: str | None = None
    attempts: list[RemixAttempt] = field(default_factory=list)
    failure_explanation: str | None = None
    suggested_retry: str | None = None
    execution_capture: RemixExecutionCapture | None = None

    def set_status(self, status: str, message: str) -> None:
        self.status = status
        self.message = message
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def snapshot(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "left_id": self.left_id,
            "right_id": self.right_id,
            "safety_mode": self.safety_mode,
            "status": self.status,
            "message": self.message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "retry_after_seconds": self.retry_after_seconds,
            "block_reason": self.block_reason,
            "rejection_reason": self.rejection_reason,
            "error": self.error,
            "embedding_error": self.embedding_error,
            "attempts": [attempt.payload() for attempt in self.attempts],
            "failure_explanation": self.failure_explanation,
            "generation_identity": generation_identity(
                capture=self.execution_capture.payload() if self.execution_capture else None,
            ),
            "suggested_retry": self.suggested_retry,
        }
        if self.tweaks:
            payload["tweaks"] = self.tweaks.payload()
        if self.result:
            payload["result"] = self.result.payload()
            payload["image_url"] = f"/api/remix/jobs/{self.id}/image"
        if self.saved_item:
            payload["saved_item"] = self.saved_item
        return payload




class RemixJobStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jobs: dict[str, RemixJob] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="remix")
        self._explanation_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="remix-explain")
        self._save_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="remix-save")
        self._maintenance_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="remix-maintenance")

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._explanation_executor.shutdown(wait=False, cancel_futures=True)
        self._save_executor.shutdown(wait=False, cancel_futures=True)
        self._maintenance_executor.shutdown(wait=False, cancel_futures=True)

    def health(self) -> dict[str, Any]:
        reason = remix_unavailable_reason(self.settings)
        return {
            "enabled": reason is None,
            "provider": self.settings.effective_remix_provider,
            "model": self.settings.remix_model,
            "aspect_ratio": self.settings.remix_aspect_ratio,
            "image_size": self.settings.remix_image_size,
            "message": reason or "Ready",
        }

    def analyze(self, left_id: int, right_id: int) -> dict[str, Any]:
        with session_scope(self.settings) as session:
            return analyze_remix_tweaks(session, self.settings, left_id, right_id).payload()

    def create_job(
        self,
        left_id: int,
        right_id: int,
        tweaks: RemixTweaks | None = None,
        safety_mode: str = "standard",
    ) -> RemixJob:
        if reason := remix_unavailable_reason(self.settings):
            raise RemixUnavailableError(reason)
        if left_id == right_id:
            raise RemixError("Choose two different memes to remix")
        try:
            capture = capture_execution(self.settings, "new_job")
        except ValueError:
            raise RemixUnavailableError("Remix configuration is incompatible. Create a new job.") from None
        job = RemixJob(
            id=uuid.uuid4().hex,
            left_id=left_id,
            right_id=right_id,
            execution_capture=capture,
            tweaks=deepcopy(remix_tweaks_from_payload(tweaks.payload())) if tweaks else None,
            safety_mode=remix_safety_mode_from_payload(safety_mode),
        )
        with self._lock:
            self._jobs[job.id] = job
        self._executor.submit(self._run_job, job.id)
        return job

    def get(self, job_id: str) -> RemixJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update_status(self, job_id: str, status: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.set_status(status, message)

    def record_attempt(self, job_id: str, attempt: RemixAttempt) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.attempts.append(attempt)
                job.updated_at = datetime.now(timezone.utc).isoformat()

    def update_attempt(
        self,
        job_id: str,
        attempt_index: int,
        *,
        status: str,
        message: str,
        error: str | None = None,
        block_reason: str | None = None,
        rejection_reason: str | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or attempt_index < 0 or attempt_index >= len(job.attempts):
                return
            attempt = job.attempts[attempt_index]
            attempt.status = status
            attempt.message = message
            attempt.error = error
            attempt.block_reason = block_reason
            attempt.rejection_reason = rejection_reason
            if rejection_reason:
                job.rejection_reason = rejection_reason
            job.updated_at = datetime.now(timezone.utc).isoformat()

    def update_attempt_rejection_reason(self, job_id: str, attempt_index: int, rejection_reason: str | None) -> None:
        reason = str(rejection_reason or "").strip()
        if not reason:
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or attempt_index < 0 or attempt_index >= len(job.attempts):
                return
            job.attempts[attempt_index].rejection_reason = reason
            job.rejection_reason = reason
            job.updated_at = datetime.now(timezone.utc).isoformat()

    def explain_blocked_attempt(
        self,
        job_id: str,
        attempt_index: int,
        context: dict[str, Any] | None,
    ) -> None:
        try:
            reason = explain_remix_rejection(self.settings, context)
        except Exception as exc:  # noqa: BLE001 - explanation should never break remix retries.
            console.print(f"[yellow]Remix rejection explanation failed:[/yellow] {exc}")
            return
        self.update_attempt_rejection_reason(job_id, attempt_index, reason)

    def save(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            if job.status == "saved" and job.saved_item:
                return {
                    "status": "saved",
                    "item": job.saved_item,
                    "embedding_error": job.embedding_error,
                }
            if job.status == "saving":
                return {"status": "saving", "job": job.snapshot()}
            if job.status != "ready" or not job.result:
                raise RemixError("Only ready remix jobs can be saved")
            job.set_status("saving", "Saving remix to arsenal")
            job.error = None
            result = job.result
            snapshot = job.snapshot()

        self._save_executor.submit(self._save_job, job_id, result)
        return {"status": "saving", "job": snapshot}

    def _save_job(self, job_id: str, result: RemixResult) -> None:
        saved_item_id: int | None = None
        try:
            with session_scope(self.settings) as session:
                saved = save_remix_result(session, self.settings, result, embed=False, export_static_catalog=False)

            saved_item_id = int(saved["item"]["id"])
            with self._lock:
                job = self._jobs.get(job_id)
                if not job:
                    return
                job.saved_item = saved["item"]
                job.embedding_error = saved.get("embedding_error")
                job.error = None
                job.set_status("saved", "Saved to arsenal")
        except Exception as exc:  # noqa: BLE001 - save errors should leave the generated remix retryable.
            console.print(f"[yellow]Remix save failed:[/yellow] {exc}")
            with self._lock:
                job = self._jobs.get(job_id)
                if job:
                    job.error = str(exc)
                    job.set_status("ready", "Save failed")
            return

        if saved_item_id is not None:
            self._maintenance_executor.submit(self._finish_saved_item, job_id, saved_item_id)

    def _finish_saved_item(self, job_id: str, media_item_id: int) -> None:
        embedding_error: str | None = None
        try:
            with session_scope(self.settings) as session:
                export_catalog(session, self.settings)
                media_item = session.get(MediaItem, media_item_id)
                if media_item:
                    try:
                        embed_media_item(session, self.settings, media_item, force=True)
                    except Exception as exc:  # noqa: BLE001 - saved remix should remain usable if embedding fails.
                        embedding_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - background maintenance should not break the saved job.
            embedding_error = str(exc)
            console.print(f"[yellow]Remix save maintenance failed:[/yellow] {exc}")
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.embedding_error = embedding_error
                job.updated_at = datetime.now(timezone.utc).isoformat()

    def _run_job(self, job_id: str) -> None:
        job = self.get(job_id)
        if not job:
            return
        self.update_status(job_id, "preparing_media", "Preparing source memes")
        try:
            with session_scope(self.settings) as session:
                plan = build_remix_mitigation_plan(
                    session,
                    job.execution_capture.apply(self.settings),
                    job.left_id,
                    job.right_id,
                    safety_mode=job.safety_mode,
                )
        except Exception as exc:  # noqa: BLE001 - API should report prep failures as job failures.
            self._finalize_failure(job_id, exc, terminal_status="failed")
            return

        last_error: Exception | None = None
        last_blocking_context: str | None = None
        for index, mitigation in enumerate(plan):
            label = mitigation.label if mitigation else "Original request"
            description = mitigation.description if mitigation else "Using source visuals and safely filtered metadata."
            attempt = RemixAttempt(
                attempt=index + 1,
                mitigation_id=mitigation.id if mitigation else "original",
                label=label,
                description=description,
                message="Calling Gemini",
            )
            self.record_attempt(job_id, attempt)
            if mitigation:
                self.update_status(job_id, "calling_gemini", mitigation.description)
            try:
                with session_scope(self.settings) as session:
                    result = generate_remix(
                        session,
                        self.settings,
                        job.left_id,
                        job.right_id,
                        job_id=job.id,
                        status_callback=lambda status, message: self.update_status(job.id, status, message),
                        tweaks=job.tweaks,
                        mitigation=mitigation,
                        failure_context=last_blocking_context,
                        safety_mode=job.safety_mode,
                        execution_capture=job.execution_capture,
                        attempt_number=index + 1,
                    )
                self.update_attempt(job_id, index, status="succeeded", message="Generated image")
                with self._lock:
                    current = self._jobs[job_id]
                    current.result = result
                    current.failure_explanation = None
                    current.suggested_retry = None
                    current.set_status(
                        "ready",
                        "Remix ready" if mitigation is None else f"Remix ready after {label.lower()}",
                    )
                return
            except GeminiRateLimitError as exc:
                if is_credit_depletion_error(exc):
                    self.update_attempt(job_id, index, status="failed", message="Credits depleted", error=str(exc))
                    self._finalize_failure(job_id, exc, terminal_status="failed")
                    return
                self.update_attempt(job_id, index, status="failed", message="Rate limited", error=str(exc))
                with self._lock:
                    current = self._jobs[job_id]
                    current.retry_after_seconds = _rate_limit_retry_after_seconds(exc)
                    current.error = str(exc)
                    current.failure_explanation = _failure_explanation(exc)
                    current.suggested_retry = _suggested_retry(exc, exhausted=False)
                    current.set_status("rate_limited", "Gemini rate limit reached")
                return
            except RemixBlockedError as exc:
                last_error = exc
                last_blocking_context = exc.failure_explanation or f"Previous attempt was blocked: {exc.block_reason or 'unknown'}"
                rejection_reason = local_remix_rejection_reason(exc.rejection_context)
                self.update_attempt(
                    job_id,
                    index,
                    status="failed",
                    message="Safety filtered",
                    error=str(exc),
                    block_reason=exc.block_reason,
                    rejection_reason=rejection_reason,
                )
                self._explanation_executor.submit(
                    self.explain_blocked_attempt,
                    job_id,
                    index,
                    exc.rejection_context,
                )
                if index + 1 < len(plan):
                    continue
                self._finalize_failure(job_id, exc, terminal_status="blocked")
                return
            except Exception as exc:  # noqa: BLE001 - continue through mitigation ladder where possible.
                last_error = exc
                rejection_reason = (
                    local_remix_rejection_reason(exc.rejection_context)
                    if isinstance(exc, RemixNoImageError)
                    else None
                )
                self.update_attempt(
                    job_id,
                    index,
                    status="failed",
                    message=_attempt_message(exc),
                    error=str(exc),
                    rejection_reason=rejection_reason,
                )
                if isinstance(exc, RemixNoImageError):
                    last_blocking_context = "Previous attempt returned text or metadata but no generated image."
                    self._explanation_executor.submit(
                        self.explain_blocked_attempt,
                        job_id,
                        index,
                        exc.rejection_context,
                    )
                if can_retry_with_mitigation(exc) and index + 1 < len(plan):
                    continue
                self._finalize_failure(job_id, exc, terminal_status="failed")
                return

        if last_error is not None:
            self._finalize_failure(job_id, last_error, terminal_status="failed")

    def _finalize_failure(self, job_id: str, exc: Exception, terminal_status: str) -> None:
        with self._lock:
            current = self._jobs[job_id]
            if isinstance(exc, RemixBlockedError):
                current.block_reason = exc.block_reason
                current.rejection_reason = current.rejection_reason or local_remix_rejection_reason(exc.rejection_context)
            if isinstance(exc, RemixNoImageError):
                current.rejection_reason = current.rejection_reason or local_remix_rejection_reason(exc.rejection_context)
            current.error = str(exc)
            current.failure_explanation = _failure_explanation(exc)
            current.suggested_retry = _suggested_retry(exc, exhausted=True)
            current.set_status(terminal_status, "Remix could not be generated")


class ArsenalDashboardHandler(SimpleHTTPRequestHandler):
    server_version = REMIX_SERVER_PRODUCT

    def __init__(self, *args: Any, settings: Settings, jobs: RemixJobStore, **kwargs: Any) -> None:
        self.settings = settings
        self.jobs = jobs
        self._range_remaining: int | None = None
        super().__init__(*args, directory=str(settings.reserve_path), **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API.
        path = _normalized_url_path(self.path)
        if path == "/":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/dashboard/dist/")
            self.end_headers()
            return
        if _is_favicon_path(path):
            self._handle_favicon()
            return
        if _is_remix_api_path(path):
            self._handle_api_get(path)
            return
        if _is_public_static_path(path):
            super().do_GET()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API.
        path = _normalized_url_path(self.path)
        if path == "/":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/dashboard/dist/")
            self.end_headers()
            return
        if _is_favicon_path(path):
            self._handle_favicon()
            return
        if _is_public_static_path(path):
            super().do_HEAD()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API.
        path = _normalized_url_path(self.path)
        if _is_remix_api_path(path):
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{self.headers.get('Host', '')}":
                self._send_json({"error": "Cross-origin write rejected"}, HTTPStatus.FORBIDDEN)
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if path in {"/api/remix/jobs", "/api/remix/analysis"} and content_type != "application/json":
                self._send_json({"error": "Expected application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return
            self._handle_api_post(path)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def list_directory(self, path: str) -> None:
        self.send_error(HTTPStatus.NOT_FOUND)
        return None

    def end_headers(self) -> None:
        for header, value in SECURITY_HEADERS.items():
            self.send_header(header, value)
        super().end_headers()

    def send_head(self) -> Any:
        self._range_remaining = None
        path = self.translate_path(self.path)
        if os.path.isdir(path) or path.endswith("/"):
            return super().send_head()

        try:
            file = open(path, "rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None

        try:
            stat_result = os.fstat(file.fileno())
            file_size = stat_result.st_size
            content_type = self.guess_type(path)
            range_header = self.headers.get("Range")

            if range_header:
                try:
                    start, end = _parse_byte_range(range_header, file_size)
                except (TypeError, ValueError):
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Content-Length", "0")
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    file.close()
                    return None

                content_length = end - start + 1
                self._range_remaining = content_length
                file.seek(start)
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-type", content_type)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(content_length))
                self.send_header("Last-Modified", self.date_time_string(stat_result.st_mtime))
                self.end_headers()
                return file

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(file_size))
            self.send_header("Last-Modified", self.date_time_string(stat_result.st_mtime))
            self.end_headers()
            return file
        except Exception:
            file.close()
            raise

    def copyfile(self, source: Any, outputfile: Any) -> None:
        if self._range_remaining is not None:
            remaining = self._range_remaining
            try:
                while remaining > 0:
                    chunk = source.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    outputfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return
            return
        try:
            super().copyfile(source, outputfile)
        except (BrokenPipeError, ConnectionResetError):
            return

    def finish(self) -> None:
        try:
            super().finish()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _handle_api_get(self, path: str) -> None:
        if path == "/api/remix/health":
            self._send_json(self.jobs.health())
            return
        parts = path.strip("/").split("/")
        if len(parts) >= 4 and parts[:3] == ["api", "remix", "jobs"]:
            job = self.jobs.get(parts[3])
            if not job:
                self._send_json({"error": "Job not found"}, HTTPStatus.NOT_FOUND)
                return
            if len(parts) == 4:
                self._send_json(job.snapshot())
                return
            if len(parts) == 5 and parts[4] == "image":
                self._send_job_image(job)
                return
        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def _handle_api_post(self, path: str) -> None:
        if path == "/api/remix/uploads":
            try:
                upload = self._read_upload_file()
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return

            upload_dir = self.settings.remix_drafts_dir
            upload_dir.mkdir(parents=True, exist_ok=True)
            suffix = Path(upload["filename"]).suffix.lower() or ".bin"
            upload_path = upload_dir / f"{uuid.uuid4().hex}{suffix}"

            try:
                upload_path.write_bytes(upload["data"])
                with session_scope(self.settings) as session:
                    upload_chat_id = _draft_upload_chat_id()
                    next_message_id = (
                        session.scalar(
                            select(func.max(TelegramMessage.telegram_message_id)).where(
                                TelegramMessage.chat_id == upload_chat_id
                            )
                        )
                        or 0
                    )
                    message = TelegramMessage(
                        telegram_message_id=int(next_message_id) + 1,
                        chat_id=upload_chat_id,
                        date=datetime.now(timezone.utc),
                        caption=Path(upload["filename"]).stem,
                        raw_metadata_json={
                            "upload_file_name": upload["filename"],
                            "upload_size_bytes": len(upload["data"]),
                        },
                        import_status="upload",
                    )
                    session.add(message)
                    session.flush()

                    media_item = import_local_media_file(
                        session,
                        self.settings,
                        message.id,
                        upload_path,
                        upload["content_type"],
                    )
                    media_item.origin = DASHBOARD_UPLOAD_ORIGIN
                    media_item.public_status = "draft"
                    media_item.visibility = "private"
                    media_item.public_original_allowed = False
                    session.flush()

                    if not process_media_item(session, self.settings, media_item):
                        detail = media_item.processing_error or "Unable to process uploaded media"
                        raise ValueError(detail)

                    try:
                        classify_media_item(session, self.settings, media_item, force=True)
                    except Exception as exc:  # noqa: BLE001 - return user-facing classification failure.
                        raise ValueError(f"Failed to classify uploaded media: {exc}") from exc

                    self._send_json(_remix_payload(session, self.settings, media_item), HTTPStatus.OK)
                    return

            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            except Exception as exc:  # noqa: BLE001 - surface unexpected upload failures as JSON.
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            finally:
                try:
                    upload_path.unlink(missing_ok=True)
                except FileNotFoundError:
                    pass

        if path == "/api/remix/analysis":
            payload = self._read_json()
            try:
                left_id = int(payload.get("left_id"))
                right_id = int(payload.get("right_id"))
                analysis = self.jobs.analyze(left_id, right_id)
            except Exception as exc:  # noqa: BLE001 - render API validation/errors as JSON.
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(analysis)
            return

        if path == "/api/remix/jobs":
            payload = self._read_json()
            try:
                left_id = int(payload.get("left_id"))
                right_id = int(payload.get("right_id"))
                tweaks = remix_tweaks_from_payload(payload.get("tweaks"))
                safety_mode = remix_safety_mode_from_payload(payload.get("safety_mode"))
                job = self.jobs.create_job(left_id, right_id, tweaks=tweaks, safety_mode=safety_mode)
            except Exception as exc:  # noqa: BLE001 - render API validation/errors as JSON.
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(job.snapshot(), HTTPStatus.ACCEPTED)
            return

        parts = path.strip("/").split("/")
        if len(parts) == 5 and parts[:3] == ["api", "remix", "jobs"] and parts[4] == "save":
            try:
                saved = self.jobs.save(parts[3])
            except KeyError:
                self._send_json({"error": "Job not found"}, HTTPStatus.NOT_FOUND)
                return
            except Exception as exc:  # noqa: BLE001 - render save errors as JSON.
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(saved)
            return

        if len(parts) == 5 and parts[:3] == ["api", "remix", "uploads"] and parts[4] == "save":
            try:
                media_item_id = int(parts[3])
            except ValueError:
                self._send_json({"error": "Invalid media item id"}, HTTPStatus.BAD_REQUEST)
                return

            with session_scope(self.settings) as session:
                media_item = session.get(MediaItem, media_item_id)
                if not media_item:
                    self._send_json({"error": "Item not found"}, HTTPStatus.NOT_FOUND)
                    return
                if media_item.origin != DASHBOARD_UPLOAD_ORIGIN:
                    self._send_json({"error": "Item not found"}, HTTPStatus.NOT_FOUND)
                    return
                if media_item.processing_status != "processed":
                    self._send_json({"error": "Uploaded media is not ready yet"}, HTTPStatus.BAD_REQUEST)
                    return

                if media_item.public_status != "published" or media_item.visibility != "public":
                    media_item.public_status = "published"
                    media_item.visibility = "public"
                    media_item.public_original_allowed = True
                    media_item.published_at = media_item.published_at or datetime.now(timezone.utc)
                    media_item.updated_at = datetime.now(timezone.utc)
                    session.flush()

                payload = _remix_payload(session, self.settings, media_item)
            self._send_json({"status": "saved", "item": payload})
            return

        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def _read_upload_file(self) -> dict[str, object]:
        content_type = self.headers.get("Content-Type")
        if not content_type or "multipart/form-data" not in content_type.lower():
            raise ValueError("Request must be multipart/form-data")
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length <= 0:
            raise ValueError("Uploaded file is empty")
        raw = self.rfile.read(content_length)
        uploaded: dict[bytes, tuple[str, bytes, str | None]] = {}

        try:
            parse_form(
                {
                    "Content-Type": content_type,
                    "Content-Length": str(content_length),
                },
                BytesIO(raw),
                on_field=lambda field: None,
                on_file=lambda file: uploaded.__setitem__(
                    file.field_name,
                    (
                        file.file_name.decode("utf-8", errors="replace"),
                        file.file_object.seek(0) or file.file_object.read(),
                        file.content_type,
                    ),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - convert parser failures into user-visible messages.
            raise ValueError(f"Failed to parse upload payload: {exc}") from exc

        if b"file" not in uploaded:
            raise ValueError("Missing file field")
        filename, data, field_content_type = uploaded[b"file"]
        if not filename:
            raise ValueError("Uploaded file is missing a name")
        if not data:
            raise ValueError("Uploaded file is empty")

        return {
            "filename": filename.strip(),
            "data": data,
            "content_type": field_content_type,
        }

    def _send_job_image(self, job: RemixJob) -> None:
        if not job.result or not job.result.image_path.exists():
            self._send_json({"error": "Image not ready"}, HTTPStatus.NOT_FOUND)
            return
        data = job.result.image_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(job.result.image_path.name)[0] or "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _handle_favicon(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        body = self.rfile.read(length).decode("utf-8")
        loaded = json.loads(body)
        if not isinstance(loaded, dict):
            raise ValueError("JSON body must be an object")
        return loaded

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return


def serve_dashboard(settings: Settings, host: str = "127.0.0.1", port: int = 8123) -> None:
    settings.dashboard_dist_dir.mkdir(parents=True, exist_ok=True)
    jobs = RemixJobStore(settings)
    handler = partial(ArsenalDashboardHandler, settings=settings, jobs=jobs)
    server = ThreadingHTTPServer((host, port), handler)
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{display_host}:{port}/dashboard/dist/"
    console.print(f"Serving {APP_NAME} dashboard at [bold]{url}[/bold]")
    console.print(f"Remix API: [bold]http://{display_host}:{port}/api/remix/health[/bold]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("Stopping dashboard server.")
    finally:
        jobs.shutdown()
        server.server_close()
