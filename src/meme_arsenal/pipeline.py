from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
import threading
import time
from typing import Any

from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from sqlalchemy import select
from sqlalchemy.orm import Session

from .ai import (
    _classification_context,
    _effective_ai_delay_seconds,
    classification_block_reason,
    classification_decision,
    classification_is_blocked,
    classify_media_item,
)
from .config import Settings
from .db import session_scope
from .embeddings import (embed_media_item, embedding_decision, embedding_source_text,
                         embedding_text_hash, select_embedding_input)
from .google_api import GeminiRateLimitError, google_work_paused, is_credit_depletion_error
from .link_preview import message_link_preview
from .logging import console
from .media import media_needs_processing, process_media_item, video_keyframes_need_backfill
from .models import MediaItem, TelegramMessage


@dataclass
class StageResult:
    media_item_id: int
    ok: bool
    status: str
    context: dict[str, Any] = field(default_factory=dict)
    rate_limited: bool = False
    retry_after_seconds: float | None = None
    quota_metric: str | None = None
    error: str | None = None
    provider_paused: bool = False


class AiPacer:
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = max(0.0, delay_seconds)
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        if self.delay_seconds <= 0:
            return
        with self._lock:
            now = time.monotonic()
            sleep_seconds = max(0.0, self._next_at - now)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
            self._next_at = time.monotonic() + self.delay_seconds


def _item_context(session: Session, media_item: MediaItem | None) -> dict[str, Any]:
    if media_item is None:
        return {}
    message = session.get(TelegramMessage, media_item.telegram_message_id)
    link_preview = message_link_preview(message)
    urls = [
        str(value)
        for key in ("url", "display_url", "embed_url")
        if (value := link_preview.get(key))
    ]
    return {
        "media_item_id": media_item.id,
        "media_type": media_item.media_type,
        "telegram_message_id": message.telegram_message_id if message else None,
        "date": message.date.isoformat() if message and message.date else None,
        "caption_snippet": (message.caption or "")[:220] if message and message.caption else "",
        "source_urls": urls,
    }


def _ordered_media_ids_from_rows(rows: list[tuple[int, int]], limit: int | None) -> list[int]:
    selected_message_ids: set[int] = set()
    selected_media_ids: list[int] = []
    for media_item_id, message_db_id in rows:
        if limit is not None and message_db_id not in selected_message_ids and len(selected_message_ids) >= limit:
            continue
        selected_message_ids.add(message_db_id)
        selected_media_ids.append(media_item_id)
    return selected_media_ids


def select_pipeline_media_ids(
    session: Session,
    settings: Settings,
    limit: int | None = None,
    media_ids: list[int] | None = None,
    force: bool = False,
    include_embeddings: bool = True,
) -> list[int]:
    if media_ids == []:
        return []
    statement = (
        select(MediaItem).join(TelegramMessage)
        .where(MediaItem.original_path.is_not(None))
        .order_by(TelegramMessage.date.desc(), TelegramMessage.telegram_message_id.desc(), MediaItem.id.desc())
    )
    if media_ids is not None:
        statement = statement.where(MediaItem.id.in_(media_ids))
    rows = []
    with session.no_autoflush:
        for item in session.scalars(statement).unique().all():
            actionable = force or media_ids is not None
            if not actionable:
                actionable = (media_needs_processing(item)
                              or video_keyframes_need_backfill(session, settings, item)
                              or classification_decision(session, settings, item)[0] == "execute")
                if not actionable and include_embeddings:
                    classification = select_embedding_input(session, settings, item)
                    if classification is not None:
                        text_hash = embedding_text_hash(embedding_source_text(session, item, classification))
                        actionable = embedding_decision(session, settings, item, text_hash)[0] == "execute"
            if actionable:
                rows.append((item.id, item.telegram_message_id))
    return _ordered_media_ids_from_rows(rows, limit)


def _process_one(settings: Settings, media_item_id: int) -> StageResult:
    with session_scope(settings) as session:
        media_item = session.get(MediaItem, media_item_id)
        if media_item is None:
            return StageResult(media_item_id, False, "missing", error="Media item is missing")
        context = _item_context(session, media_item)
        if not media_needs_processing(media_item) and not video_keyframes_need_backfill(
            session, settings, media_item
        ):
            return StageResult(media_item_id, True, "already_processed", context=context)
        ok = process_media_item(session, settings, media_item)
        context = _item_context(session, media_item)
        if ok:
            return StageResult(media_item_id, True, "processed", context=context)
        return StageResult(
            media_item_id,
            False,
            "failed",
            context={**context, "processing_error": media_item.processing_error},
            error=media_item.processing_error,
        )


def _classify_one(settings: Settings, media_item_id: int, pacer: AiPacer, force: bool) -> StageResult:
    with session_scope(settings) as session:
        media_item = session.get(MediaItem, media_item_id)
        if media_item is None:
            return StageResult(media_item_id, False, "missing", error="Media item is missing")
        if media_item.processing_status != "processed":
            return StageResult(
                media_item_id,
                False,
                "skipped",
                context=_item_context(session, media_item),
                error=f"Media item is {media_item.processing_status}",
            )
        try:
            action, _ = classification_decision(session, settings, media_item, force)
            if action == "reuse":
                return StageResult(media_item_id, True, "already_classified", context=_classification_context(session, media_item))
            if action == "unknown":
                return StageResult(media_item_id, False, "withheld", context=_item_context(session, media_item),
                                   error="AI classification evidence is unknown or incomplete")
            pacer.wait()
            classification = classify_media_item(session, settings, media_item, force=force)
            if classification:
                context = _classification_context(session, media_item)
                if classification_is_blocked(classification):
                    context["block_reason"] = classification_block_reason(classification)
                    return StageResult(media_item_id, True, "blocked", context=context)
                return StageResult(media_item_id, True, "classified", context=context)
            return StageResult(
                media_item_id,
                False,
                "failed",
                context=_classification_context(session, media_item),
                error="No classification was produced",
            )
        except GeminiRateLimitError as exc:
            context = _classification_context(session, media_item)
            context.update(
                {
                    "error": str(exc),
                    "status_code": exc.status_code,
                    "status": exc.status,
                    "retry_after_seconds": exc.retry_after_seconds,
                    "quota_metric": exc.quota_metric,
                }
            )
            return StageResult(
                media_item_id,
                False,
                "rate_limited",
                context=context,
                rate_limited=True,
                provider_paused=is_credit_depletion_error(exc),
                retry_after_seconds=exc.retry_after_seconds,
                quota_metric=exc.quota_metric,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            context = _classification_context(session, media_item)
            context["error"] = str(exc)
            return StageResult(media_item_id, False, "failed", context=context, error=str(exc))


def _embed_one(settings: Settings, media_item_id: int, force: bool) -> StageResult:
    with session_scope(settings) as session:
        media_item = session.get(MediaItem, media_item_id)
        if media_item is None:
            return StageResult(media_item_id, False, "missing", error="Media item is missing")
        context = _item_context(session, media_item)
        try:
            classification = select_embedding_input(session, settings, media_item)
            if classification is None:
                return StageResult(media_item_id, True, "skipped", context=context)
            text_hash = embedding_text_hash(embedding_source_text(session, media_item, classification))
            action, _ = embedding_decision(session, settings, media_item, text_hash, force)
            if action != "execute":
                return StageResult(media_item_id, True, "skipped", context=context)
            embedding = embed_media_item(session, settings, media_item, force=force)
            if embedding:
                return StageResult(media_item_id, True, "embedded", context=context)
            return StageResult(media_item_id, True, "skipped", context=context, error="No classification is available")
        except GeminiRateLimitError as exc:
            context.update(
                {
                    "error": str(exc),
                    "status_code": exc.status_code,
                    "status": exc.status,
                    "retry_after_seconds": exc.retry_after_seconds,
                    "quota_metric": exc.quota_metric,
                }
            )
            return StageResult(
                media_item_id,
                False,
                "rate_limited",
                context=context,
                rate_limited=True,
                provider_paused=is_credit_depletion_error(exc),
                retry_after_seconds=exc.retry_after_seconds,
                quota_metric=exc.quota_metric,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            context["error"] = str(exc)
            return StageResult(media_item_id, False, "failed", context=context, error=str(exc))


def _progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    )


def _append_failure(report: dict[str, Any], key: str, result: StageResult) -> None:
    items = report.setdefault(key, [])
    if isinstance(items, list):
        items.append(result.context or {"media_item_id": result.media_item_id, "error": result.error})
    if result.error:
        report["last_error"] = result.error


def run_media_pipeline(
    settings: Settings,
    limit: int | None = None,
    media_ids: list[int] | None = None,
    process_workers: int = 6,
    classify_workers: int = 3,
    embed_workers: int = 3,
    force: bool = False,
    ai_paused: bool = False,
    embed_enabled: bool = True,
) -> dict[str, Any]:
    with session_scope(settings) as session:
        selected_ids = select_pipeline_media_ids(
            session,
            settings,
            limit=limit,
            media_ids=media_ids,
            force=force,
            include_embeddings=embed_enabled,
        )

    report: dict[str, Any] = {
        "selected_media_items": len(selected_ids),
        "media_item_ids": selected_ids,
        "limit_messages": limit,
        "process_workers": max(1, process_workers),
        "classify_workers": max(1, classify_workers),
        "embed_workers": max(1, embed_workers),
        "ai_paused": ai_paused,
        "embed_enabled": embed_enabled,
        "ai_request_delay_seconds": _effective_ai_delay_seconds(settings),
        "processed": 0,
        "process_skipped": 0,
        "process_failed": 0,
        "classified": 0,
        "classify_blocked": 0,
        "classify_skipped": 0,
        "classify_failed": 0,
        "embedded": 0,
        "embed_skipped": 0,
        "embed_failed": 0,
        "rate_limited": False,
        "stopped_early": False,
        "process_failed_items": [],
        "classify_failed_items": [],
        "embed_failed_items": [],
    }
    if not selected_ids:
        return report

    pacer = AiPacer(_effective_ai_delay_seconds(settings))
    google_paused = (
        settings.effective_classification_provider == "google"
        or (embed_enabled and settings.effective_embedding_provider == "google")
    ) and google_work_paused(settings)
    classify_paused = google_paused and settings.effective_classification_provider == "google"
    stop_classify = ai_paused or classify_paused
    stop_embed = ai_paused or not embed_enabled or (
        google_paused and settings.effective_embedding_provider == "google"
    )
    report["classification_provider_paused"] = classify_paused
    report["embedding_provider_paused"] = google_paused and settings.effective_embedding_provider == "google"

    with (
        ThreadPoolExecutor(max_workers=max(1, process_workers)) as process_pool,
        ThreadPoolExecutor(max_workers=max(1, classify_workers)) as classify_pool,
        ThreadPoolExecutor(max_workers=max(1, embed_workers)) as embed_pool,
        _progress() as progress,
    ):
        process_task = progress.add_task("Processing media", total=len(selected_ids))
        classify_task = progress.add_task("Classifying media", total=len(selected_ids))
        embed_task = progress.add_task("Embedding media", total=len(selected_ids))

        process_futures: dict[Future[StageResult], int] = {
            process_pool.submit(_process_one, settings, media_item_id): media_item_id
            for media_item_id in selected_ids
        }
        classify_futures: dict[Future[StageResult], int] = {}
        embed_futures: dict[Future[StageResult], int] = {}

        def submit_embedding(media_item_id: int) -> None:
            if not stop_embed:
                embed_futures[embed_pool.submit(_embed_one, settings, media_item_id, force)] = media_item_id
            else:
                report["embed_skipped"] += 1
                progress.advance(embed_task)

        def pause_google() -> None:
            nonlocal stop_classify, stop_embed, classify_paused
            if settings.effective_classification_provider == "google":
                stop_classify = classify_paused = True
                report["classification_provider_paused"] = True
            if settings.effective_embedding_provider == "google":
                stop_embed = True
                report["embedding_provider_paused"] = True
            report["stopped_early"] = True
            if stop_classify:
                for pending, item_id in list(classify_futures.items()):
                    if pending.cancel():
                        classify_futures.pop(pending)
                        report["classify_skipped"] += 1
                        progress.advance(classify_task)
                        submit_embedding(item_id)
            if stop_embed:
                for pending in list(embed_futures):
                    if pending.cancel():
                        embed_futures.pop(pending)
                        report["embed_skipped"] += 1
                        progress.advance(embed_task)

        while process_futures or classify_futures or embed_futures:
            done, _pending = wait(
                set(process_futures) | set(classify_futures) | set(embed_futures),
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                if future in process_futures:
                    media_item_id = process_futures.pop(future)
                    result = future.result()
                    progress.advance(process_task)
                    if result.ok:
                        if result.status == "processed":
                            report["processed"] += 1
                        else:
                            report["process_skipped"] += 1
                        if not stop_classify:
                            classify_futures[classify_pool.submit(_classify_one, settings, media_item_id, pacer, force)] = media_item_id
                        else:
                            report["classify_skipped"] += 1
                            progress.advance(classify_task)
                            if classify_paused and not ai_paused:
                                submit_embedding(media_item_id)
                            else:
                                report["embed_skipped"] += 1
                                progress.advance(embed_task)
                    else:
                        report["process_failed"] += 1
                        _append_failure(report, "process_failed_items", result)
                        progress.advance(classify_task)
                        progress.advance(embed_task)
                elif future in classify_futures:
                    media_item_id = classify_futures.pop(future)
                    result = future.result()
                    if result.provider_paused:
                        pause_google()
                    progress.advance(classify_task)
                    if result.ok:
                        if result.status == "classified":
                            report["classified"] += 1
                        elif result.status == "blocked":
                            report["classify_blocked"] += 1
                        else:
                            report["classify_skipped"] += 1
                        if not stop_embed:
                            embed_futures[embed_pool.submit(_embed_one, settings, media_item_id, force)] = media_item_id
                        else:
                            report["embed_skipped"] += 1
                            progress.advance(embed_task)
                    elif result.status == "withheld":
                        report["classify_skipped"] += 1
                        if not stop_embed:
                            embed_futures[embed_pool.submit(_embed_one, settings, media_item_id, force)] = media_item_id
                        else:
                            report["embed_skipped"] += 1
                            progress.advance(embed_task)
                    elif result.status == "skipped":
                        report["classify_skipped"] += 1
                        report["embed_skipped"] += 1
                        progress.advance(embed_task)
                    else:
                        report["classify_failed"] += 1
                        _append_failure(report, "classify_failed_items", result)
                        if result.provider_paused:
                            submit_embedding(media_item_id)
                        else:
                            progress.advance(embed_task)
                        if result.rate_limited:
                            report["rate_limited"] = True
                            report["retry_after_seconds"] = result.retry_after_seconds
                            report["quota_metric"] = result.quota_metric or ""
                            if settings.ai_stop_on_rate_limit and not result.provider_paused:
                                stop_classify = True
                                report["stopped_early"] = True
                                for pending in list(classify_futures):
                                    if pending.cancel():
                                        classify_futures.pop(pending, None)
                                        report["classify_skipped"] += 1
                                        report["embed_skipped"] += 1
                                        progress.advance(classify_task)
                                        progress.advance(embed_task)
                    progress.update(classify_task, description=f"Classifying media={media_item_id}")
                elif future in embed_futures:
                    media_item_id = embed_futures.pop(future)
                    result = future.result()
                    if result.provider_paused:
                        pause_google()
                    progress.advance(embed_task)
                    if result.ok:
                        if result.status == "embedded":
                            report["embedded"] += 1
                        else:
                            report["embed_skipped"] += 1
                    else:
                        report["embed_failed"] += 1
                        _append_failure(report, "embed_failed_items", result)
                        if result.rate_limited:
                            report["rate_limited"] = True
                            report["retry_after_seconds"] = result.retry_after_seconds
                            report["quota_metric"] = result.quota_metric or ""
                            if settings.ai_stop_on_rate_limit and not result.provider_paused:
                                stop_embed = True
                                report["stopped_early"] = True
                                for pending in list(embed_futures):
                                    if pending.cancel():
                                        embed_futures.pop(pending, None)
                                        report["embed_skipped"] += 1
                                        progress.advance(embed_task)
                    progress.update(embed_task, description=f"Embedding media={media_item_id}")

    return report
