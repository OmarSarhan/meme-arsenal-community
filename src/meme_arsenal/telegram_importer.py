from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings
from .link_preview import extract_link_preview_from_media, merge_link_preview_metadata
from .logging import console
from .media import infer_media_type, path_sha256, reserve_relative
from .models import ImportCheckpoint, ImportFailure, MediaItem, TelegramMessage


URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_json_safe(item) for item in value]
        return str(value)


def _date_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _caption_urls(caption: str | None) -> list[str]:
    if not caption:
        return []
    return [match.rstrip(".,;:!?)\"]'") for match in URL_RE.findall(caption)]


def _clean_url(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text if text.startswith(("http://", "https://")) else None


def _clean_username(value: Any) -> str | None:
    if value is None:
        return None
    username = str(value).strip().lstrip("@")
    return username or None


def _entity_username(entity: Any) -> str | None:
    return _clean_username(getattr(entity, "username", None))


def _peer_kind(peer: Any) -> str:
    if isinstance(peer, dict):
        return str(peer.get("_") or "")
    return peer.__class__.__name__ if peer is not None else ""


def _peer_numeric_id(peer: Any) -> int | None:
    if peer is None:
        return None
    if isinstance(peer, dict):
        for key in ("channel_id", "chat_id", "user_id"):
            value = peer.get(key)
            if value is not None:
                return int(value)
        return None
    for key in ("channel_id", "chat_id", "user_id"):
        value = getattr(peer, key, None)
        if value is not None:
            return int(value)
    return None


def _post_url_from_peer(peer: Any, message_id: int | None, username: str | None = None) -> str | None:
    if not message_id:
        return None
    kind = _peer_kind(peer)
    if username and kind != "PeerUser":
        return f"https://t.me/{username}/{message_id}"
    if kind == "PeerChannel":
        peer_id = _peer_numeric_id(peer)
        if peer_id:
            return f"https://t.me/c/{peer_id}/{message_id}"
    return None


def _telegram_post_url_from_metadata(metadata: dict[str, Any] | None, message_id: int | None = None) -> str | None:
    if not metadata:
        return None
    explicit = _clean_url(metadata.get("telegram_post_url") or metadata.get("post_url"))
    if explicit:
        return explicit

    direct = _post_url_from_peer(metadata.get("peer_id"), message_id)
    if direct:
        return direct

    forwarded = metadata.get("fwd_from")
    if not isinstance(forwarded, dict):
        return None
    forwarded_message_id = forwarded.get("channel_post") or forwarded.get("saved_from_msg_id")
    if forwarded_message_id is None:
        return None
    return _post_url_from_peer(
        forwarded.get("from_id") or forwarded.get("saved_from_peer"),
        int(forwarded_message_id),
    )


def _telegram_post_url_from_message(raw_message: Any, raw_metadata: dict[str, Any] | None = None) -> str | None:
    explicit = _clean_url(getattr(raw_message, "link", None))
    if explicit:
        return explicit

    message_id = getattr(raw_message, "id", None)
    direct = _post_url_from_peer(
        getattr(raw_message, "peer_id", None),
        int(message_id) if message_id is not None else None,
        _entity_username(getattr(raw_message, "chat", None)),
    )
    if direct:
        return direct

    forward = getattr(raw_message, "forward", None)
    forwarded_message_id = getattr(forward, "channel_post", None) or getattr(forward, "saved_from_msg_id", None)
    if forwarded_message_id:
        forwarded = _post_url_from_peer(
            getattr(forward, "from_id", None) or getattr(forward, "saved_from_peer", None),
            int(forwarded_message_id),
            _entity_username(getattr(forward, "chat", None)),
        )
        if forwarded:
            return forwarded

    return _telegram_post_url_from_metadata(raw_metadata, int(message_id) if message_id is not None else None)


def _message_context(
    raw_message: Any,
    link_preview: dict[str, Any] | None = None,
    stage: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    caption = getattr(raw_message, "message", None)
    urls = _caption_urls(caption)
    if link_preview:
        for key in ("url", "display_url", "embed_url"):
            value = link_preview.get(key)
            if value and str(value) not in urls:
                urls.append(str(value))
    context: dict[str, Any] = {
        "stage": stage,
        "telegram_message_id": getattr(raw_message, "id", None),
        "date": _date_iso(getattr(raw_message, "date", None)),
        "caption_snippet": (caption or "")[:220],
        "urls": urls,
        "telegram_post_url": _telegram_post_url_from_message(raw_message),
        "link_preview": link_preview or None,
    }
    if error:
        context["error"] = error
    return {key: value for key, value in context.items() if value not in (None, "", [])}


def _fixture_context(
    entry: dict[str, Any],
    stage: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    link_preview = entry.get("link_preview") if isinstance(entry.get("link_preview"), dict) else None
    urls = _caption_urls(entry.get("caption"))
    if link_preview:
        for key in ("url", "display_url", "embed_url"):
            value = link_preview.get(key)
            if value and str(value) not in urls:
                urls.append(str(value))
    context: dict[str, Any] = {
        "stage": stage,
        "telegram_message_id": entry.get("telegram_message_id"),
        "date": entry.get("date"),
        "caption_snippet": (entry.get("caption") or "")[:220],
        "urls": urls,
        "link_preview": link_preview,
    }
    if error:
        context["error"] = error
    return {key: value for key, value in context.items() if value not in (None, "", [])}


def _failure_error(error: str, context: dict[str, Any]) -> str:
    urls = context.get("urls") or []
    date = context.get("date")
    pieces = [error]
    if date:
        pieces.append(f"date={date}")
    if urls:
        pieces.append(f"urls={','.join(urls)}")
    return " | ".join(pieces)


def _flood_wait_seconds(exc: Exception) -> int | None:
    seconds = getattr(exc, "seconds", None)
    if seconds is not None and exc.__class__.__name__ == "FloodWaitError":
        return int(seconds)
    return None


def get_checkpoint(session: Session, chat_id: str = "me") -> ImportCheckpoint:
    checkpoint = session.scalar(
        select(ImportCheckpoint).where(
            ImportCheckpoint.source == "telegram_saved",
            ImportCheckpoint.chat_id == chat_id,
        )
    )
    if checkpoint is None:
        checkpoint = ImportCheckpoint(source="telegram_saved", chat_id=chat_id, last_message_id=0)
        session.add(checkpoint)
        session.flush()
    return checkpoint


def _message_exists(session: Session, chat_id: str, telegram_message_id: int) -> bool:
    return session.scalar(
        select(TelegramMessage.id).where(
            TelegramMessage.chat_id == chat_id,
            TelegramMessage.telegram_message_id == telegram_message_id,
        )
    ) is not None


def _imported_message_bounds(session: Session, chat_id: str) -> tuple[int | None, int | None]:
    return session.execute(
        select(
            func.min(TelegramMessage.telegram_message_id),
            func.max(TelegramMessage.telegram_message_id),
        ).where(TelegramMessage.chat_id == chat_id)
    ).one()


def _entry_sort_key(entry: dict[str, Any]) -> tuple[datetime, int]:
    date_raw = entry.get("date")
    if date_raw:
        try:
            date = datetime.fromisoformat(str(date_raw))
        except ValueError:
            date = datetime.min.replace(tzinfo=timezone.utc)
    else:
        date = datetime.min.replace(tzinfo=timezone.utc)
    return date, int(entry.get("telegram_message_id") or 0)


def _raw_message_date_sort_key(raw_message: Any) -> datetime:
    date = getattr(raw_message, "date", None)
    if not isinstance(date, datetime):
        return datetime.min.replace(tzinfo=timezone.utc)
    if date.tzinfo is None:
        return date.replace(tzinfo=timezone.utc)
    return date


def record_failure(
    session: Session,
    stage: str,
    error: str,
    telegram_message_id: int | None = None,
) -> None:
    session.add(
        ImportFailure(
            telegram_message_id=telegram_message_id,
            stage=stage,
            error=error,
            retry_count=0,
            status="open",
        )
    )


async def _sleep_if_configured(settings: Settings) -> None:
    if settings.import_delay_seconds > 0:
        await asyncio.sleep(settings.import_delay_seconds)


def upsert_message(
    session: Session,
    telegram_message_id: int,
    chat_id: str,
    date: datetime | None,
    caption: str | None,
    grouped_id: str | None = None,
    forwarded_from: str | None = None,
    telegram_post_url: str | None = None,
    raw_metadata_json: dict[str, Any] | None = None,
) -> TelegramMessage:
    existing = session.scalar(
        select(TelegramMessage).where(
            TelegramMessage.telegram_message_id == telegram_message_id,
            TelegramMessage.chat_id == chat_id,
        )
    )
    message = existing or TelegramMessage(
        telegram_message_id=telegram_message_id,
        chat_id=chat_id,
    )
    message.date = date
    message.caption = caption
    message.grouped_id = grouped_id
    message.forwarded_from = forwarded_from
    message.telegram_post_url = telegram_post_url
    message.raw_metadata_json = raw_metadata_json or {}
    message.import_status = "imported"
    if existing is None:
        session.add(message)
    session.flush()
    return message


def attach_downloaded_media(
    session: Session,
    settings: Settings,
    message: TelegramMessage,
    downloaded_path: Path,
    mime_type: str | None = None,
) -> MediaItem:
    downloaded_path = downloaded_path.resolve()
    existing = session.scalar(
        select(MediaItem).where(
            MediaItem.telegram_message_id == message.id,
            MediaItem.original_path == reserve_relative(settings, downloaded_path),
        )
    )
    if existing:
        return existing

    item = MediaItem(
        telegram_message_id=message.id,
        media_type=infer_media_type(downloaded_path, mime_type),
        mime_type=mime_type or mimetypes.guess_type(downloaded_path.name)[0],
        original_path=reserve_relative(settings, downloaded_path),
        size_bytes=downloaded_path.stat().st_size if downloaded_path.exists() else None,
        sha256=path_sha256(downloaded_path) if downloaded_path.exists() else None,
        processing_status="pending",
    )
    session.add(item)
    session.flush()
    return item


async def download_message_media(
    client: Any,
    raw_message: Any,
    target_dir: Path,
    settings: Settings,
) -> Path | None:
    target_dir.mkdir(parents=True, exist_ok=True)
    media = getattr(raw_message, "media", None)
    webpage = getattr(media, "webpage", None)
    attempts = 0
    while attempts < 2:
        attempts += 1
        try:
            if webpage is not None:
                preview_media = getattr(webpage, "document", None) or getattr(webpage, "photo", None)
                if preview_media is not None:
                    downloaded = await client.download_media(preview_media, file=str(target_dir))
                    if downloaded:
                        await _sleep_if_configured(settings)
                        return Path(downloaded)

            downloaded = await client.download_media(raw_message, file=str(target_dir))
            await _sleep_if_configured(settings)
            return Path(downloaded) if downloaded else None
        except Exception as exc:
            wait_seconds = _flood_wait_seconds(exc)
            if wait_seconds is None or attempts >= 2:
                raise
            console.print(f"Telegram requested a pause; waiting {wait_seconds}s before retrying.")
            await asyncio.sleep(wait_seconds + 1)
    return None


def _progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    )


async def telegram_login(settings: Settings) -> None:
    from telethon import TelegramClient

    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise ValueError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")
    client = TelegramClient(
        str(settings.telegram_session_path),
        int(settings.telegram_api_id),
        settings.telegram_api_hash,
    )
    await client.start()
    await client.disconnect()


async def import_saved_messages_async(
    session: Session,
    settings: Settings,
    limit: int,
    *,
    backfill_older: bool = True,
) -> dict[str, Any]:
    from telethon import TelegramClient

    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise ValueError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")

    checkpoint = get_checkpoint(session, chat_id="me")
    client = TelegramClient(
        str(settings.telegram_session_path),
        int(settings.telegram_api_id),
        settings.telegram_api_hash,
    )
    await client.start()

    report: dict[str, Any] = {
        "messages": 0,
        "media": 0,
        "link_previews": 0,
        "skipped": 0,
        "failed": 0,
        "import_delay_seconds": settings.import_delay_seconds,
        "oldest_message_date": None,
        "newest_message_date": None,
        "last_seen_message_id": checkpoint.last_message_id,
        "telegram_message_ids": [],
        "media_item_ids": [],
        "skipped_items": [],
        "failed_items": [],
    }
    newest_id = checkpoint.last_message_id
    try:
        messages: list[Any] = []
        oldest_imported_id, newest_imported_id = _imported_message_bounds(session, "me")

        async def collect_messages(**kwargs: Any) -> None:
            nonlocal messages
            async for message in client.iter_messages("me", **kwargs):
                message_id = int(message.id)
                message_date = _date_iso(getattr(message, "date", None)) or "unknown date"
                progress.update(
                    scan_task,
                    completed=len(messages),
                    description=f"Scanning {message_date} id={message_id}",
                )
                if _message_exists(session, "me", message_id):
                    report["skipped"] += 1
                    continue
                messages.append(message)
                progress.update(scan_task, completed=len(messages))
                if len(messages) >= limit:
                    break

        with _progress() as progress:
            scan_task = progress.add_task("Scanning newest -> older", total=limit)
            try:
                retry_ids = session.scalars(
                    select(TelegramMessage.telegram_message_id).where(
                        TelegramMessage.chat_id == "me",
                        (TelegramMessage.import_status == "failed") | (
                            ~TelegramMessage.media_items.any()
                            & TelegramMessage.telegram_message_id.in_(
                                select(ImportFailure.telegram_message_id).where(
                                    ImportFailure.stage == "telegram_import"
                                )
                            )
                        ),
                    ).order_by(TelegramMessage.telegram_message_id).limit(limit)
                ).all()
                if retry_ids:
                    messages.extend(m for m in await client.get_messages("me", ids=retry_ids) if m)
                if newest_imported_id and len(messages) < limit:
                    await collect_messages(
                        min_id=int(newest_imported_id),
                        limit=limit - len(messages),
                        reverse=True,
                    )
                elif not newest_imported_id and not backfill_older and len(messages) < limit:
                    await collect_messages(limit=limit)
                if backfill_older and len(messages) < limit:
                    older_kwargs: dict[str, Any] = {"limit": limit - len(messages)}
                    if oldest_imported_id:
                        older_kwargs["max_id"] = int(oldest_imported_id)
                    await collect_messages(**older_kwargs)
            except Exception as exc:
                wait_seconds = _flood_wait_seconds(exc)
                if wait_seconds is not None:
                    console.print(f"Telegram requested a pause while scanning; waiting {wait_seconds}s.")
                    await asyncio.sleep(wait_seconds + 1)
                report["failed"] += 1
                context = {
                    "stage": "telegram_scan",
                    "checkpoint_message_id": checkpoint.last_message_id,
                    "error": str(exc),
                }
                report["failed_items"].append(context)
                record_failure(session, "telegram_scan", str(exc), None)
                session.flush()
                return report

            if messages:
                dates = [message.date for message in messages if getattr(message, "date", None)]
                if dates:
                    report["oldest_message_date"] = min(dates).isoformat()
                    report["newest_message_date"] = max(dates).isoformat()
                report["last_seen_message_id"] = max(int(message.id) for message in messages)

            import_task = progress.add_task("Importing oldest -> newest", total=len(messages))
            for raw_message in sorted(messages, key=_raw_message_date_sort_key):
                link_preview: dict[str, Any] | None = None
                saved = None
                try:
                    message_date = _date_iso(getattr(raw_message, "date", None)) or "unknown date"
                    progress.update(
                        import_task,
                        description=f"Importing {message_date} id={getattr(raw_message, 'id', '?')}",
                    )
                    raw_dict = raw_message.to_dict() if hasattr(raw_message, "to_dict") else {}
                    link_preview = extract_link_preview_from_media(getattr(raw_message, "media", None))
                    if link_preview:
                        report["link_previews"] += 1
                    metadata = _json_safe(merge_link_preview_metadata(raw_dict, link_preview))
                    telegram_post_url = _telegram_post_url_from_message(raw_message, metadata)
                    if telegram_post_url:
                        metadata["telegram_post_url"] = telegram_post_url
                    saved = upsert_message(
                        session=session,
                        telegram_message_id=int(raw_message.id),
                        chat_id="me",
                        date=raw_message.date,
                        caption=getattr(raw_message, "message", None),
                        grouped_id=str(getattr(raw_message, "grouped_id", "") or "") or None,
                        forwarded_from=str(getattr(raw_message, "fwd_from", "") or "") or None,
                        telegram_post_url=telegram_post_url,
                        raw_metadata_json=metadata,
                    )
                    report["messages"] += 1
                    report["telegram_message_ids"].append(int(raw_message.id))
                    newest_id = max(newest_id, int(raw_message.id))
                    if getattr(raw_message, "media", None):
                        target_dir = settings.originals_dir / "telegram" / str(raw_message.id)
                        target_dir.mkdir(parents=True, exist_ok=True)
                        downloaded = await download_message_media(client, raw_message, target_dir, settings)
                        if downloaded:
                            media_item = attach_downloaded_media(session, settings, saved, downloaded)
                            report["media_item_ids"].append(media_item.id)
                            report["media"] += 1
                        else:
                            report["skipped"] += 1
                            context = _message_context(raw_message, link_preview, stage="download_media")
                            report["skipped_items"].append(context)
                    await _sleep_if_configured(settings)
                    saved.import_status = "imported"
                    progress.advance(import_task)
                    session.flush()
                except Exception as exc:  # noqa: BLE001 - importer records item failures and continues.
                    if saved is not None:
                        saved.import_status = "failed"
                    report["failed"] += 1
                    context = _message_context(raw_message, link_preview, stage="telegram_import", error=str(exc))
                    report["failed_items"].append(context)
                    record_failure(
                        session,
                        "telegram_import",
                        _failure_error(str(exc), context),
                        getattr(raw_message, "id", None),
                    )
                    progress.advance(import_task)
        checkpoint.last_message_id = newest_id
        session.flush()
    finally:
        await client.disconnect()
    return report


def import_saved_messages(
    session: Session,
    settings: Settings,
    limit: int,
    *,
    backfill_older: bool = True,
) -> dict[str, Any]:
    return asyncio.run(
        import_saved_messages_async(
            session,
            settings,
            limit=limit,
            backfill_older=backfill_older,
        )
    )


def telegram_login_sync(settings: Settings) -> None:
    asyncio.run(telegram_login(settings))


def backfill_telegram_post_urls(session: Session) -> dict[str, int]:
    messages = session.scalars(
        select(TelegramMessage).where(TelegramMessage.telegram_post_url.is_(None))
    ).all()
    counts = {"checked": len(messages), "backfilled": 0}
    for message in messages:
        metadata = dict(message.raw_metadata_json or {})
        telegram_post_url = _telegram_post_url_from_metadata(metadata, message.telegram_message_id)
        if not telegram_post_url:
            continue
        message.telegram_post_url = telegram_post_url
        metadata["telegram_post_url"] = telegram_post_url
        message.raw_metadata_json = metadata
        counts["backfilled"] += 1
    session.flush()
    return counts


def import_fixture_messages(
    session: Session,
    settings: Settings,
    fixture_dir: Path,
    limit: int | None = None,
) -> dict[str, Any]:
    fixture_dir = fixture_dir.resolve()
    manifest_path = fixture_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = manifest.get("messages", [])
    else:
        entries = [
            {
                "telegram_message_id": index + 1,
                "caption": path.stem.replace("-", " "),
                "file": path.name,
                "date": datetime.now(timezone.utc).isoformat(),
            }
            for index, path in enumerate(sorted(fixture_dir.iterdir()))
            if path.is_file() and not path.name.startswith(".")
        ]

    counts: dict[str, Any] = {
        "messages": 0,
        "media": 0,
        "link_previews": 0,
        "skipped": 0,
        "failed": 0,
        "telegram_message_ids": [],
        "media_item_ids": [],
        "skipped_items": [],
        "failed_items": [],
    }
    checkpoint = get_checkpoint(session, chat_id="fixture")
    imported = 0
    for entry in sorted(entries, key=_entry_sort_key, reverse=True):
        if limit is not None and imported >= limit:
            break
        try:
            telegram_message_id = int(entry["telegram_message_id"])
            if _message_exists(session, "fixture", telegram_message_id):
                counts["skipped"] += 1
                continue
            date_raw = entry.get("date")
            date = datetime.fromisoformat(date_raw) if date_raw else datetime.now(timezone.utc)
            link_preview = entry.get("link_preview") if isinstance(entry.get("link_preview"), dict) else None
            if link_preview:
                counts["link_previews"] += 1
            telegram_post_url = _clean_url(entry.get("telegram_post_url") or entry.get("post_url"))
            metadata = merge_link_preview_metadata({"fixture": entry}, link_preview)
            if telegram_post_url:
                metadata["telegram_post_url"] = telegram_post_url
            message = upsert_message(
                session=session,
                telegram_message_id=telegram_message_id,
                chat_id="fixture",
                date=date,
                caption=entry.get("caption"),
                telegram_post_url=telegram_post_url,
                raw_metadata_json=metadata,
            )
            counts["messages"] += 1
            imported += 1
            counts["telegram_message_ids"].append(telegram_message_id)
            file_name = entry.get("file")
            source = fixture_dir / file_name if file_name else None
            if source and source.exists():
                target_dir = settings.originals_dir / "fixtures" / str(telegram_message_id)
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / source.name
                shutil.copy2(source, target)
                media_item = attach_downloaded_media(
                    session,
                    settings,
                    message,
                    target,
                    mime_type=entry.get("mime_type") or mimetypes.guess_type(target.name)[0],
                )
                counts["media_item_ids"].append(media_item.id)
                counts["media"] += 1
            elif link_preview:
                counts["skipped"] += 1
                counts["skipped_items"].append(_fixture_context(entry, stage="fixture_missing_media"))
            checkpoint.last_message_id = max(checkpoint.last_message_id, telegram_message_id)
            session.flush()
        except Exception as exc:  # noqa: BLE001
            counts["failed"] += 1
            context = _fixture_context(entry, stage="fixture_import", error=str(exc))
            counts["failed_items"].append(context)
            record_failure(
                session,
                "fixture_import",
                _failure_error(str(exc), context),
                entry.get("telegram_message_id"),
            )
    return counts
