from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import mimetypes
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import imagehash
from PIL import Image, ImageOps, UnidentifiedImageError
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .config import Settings
from .db import session_scope
from .link_preview import message_link_preview
from .logging import console
from .models import MediaItem, MediaKeyframe, TelegramMessage


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".gifv", ".mpg", ".mpeg"}
STICKER_EXTENSIONS = {".webp", ".tgs", ".webm"}
BROWSER_PLAYBACK_VERSION = 2
FFMPEG_TIMEOUT_SECONDS = 300.0
FFPROBE_TIMEOUT_SECONDS = 30.0
PLAYBACK_LOCK_TIMEOUT_SECONDS = FFMPEG_TIMEOUT_SECONDS + 15.0


def path_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def infer_media_type(path: Path, mime_type: str | None = None) -> str:
    lower = path.suffix.lower()
    mime_type = mime_type or mimetypes.guess_type(path.name)[0] or ""
    if mime_type.startswith("image/") or lower in IMAGE_EXTENSIONS:
        if lower == ".gif" or mime_type == "image/gif":
            return "gif"
        return "image"
    if mime_type.startswith("video/") or lower in VIDEO_EXTENSIONS:
        return "video"
    if lower in STICKER_EXTENSIONS:
        return "sticker"
    return "document"


def reserve_relative(settings: Settings, path: Path) -> str:
    return path.resolve().relative_to(settings.reserve_path.resolve()).as_posix()


def reserve_absolute(settings: Settings, stored_path: str | None) -> Path | None:
    if not stored_path:
        return None
    relative = str(stored_path).replace("\\", "/").lstrip("/")
    absolute = (settings.reserve_path / relative).resolve()
    try:
        absolute.relative_to(settings.reserve_path.resolve())
    except ValueError:
        return None
    return absolute


def stored_media_file_exists(settings: Settings, stored_path: str | None) -> bool:
    absolute = reserve_absolute(settings, stored_path)
    return bool(absolute and absolute.exists() and absolute.is_file())


def media_item_has_original_file(settings: Settings, media_item: MediaItem) -> bool:
    return stored_media_file_exists(settings, media_item.original_path)


def establish_derivative_hashes(settings: Settings, media_item: MediaItem) -> None:
    """Explicit local establishment for legacy processed items; never called by lookup.

    Original and frame identities retain their existing intake/processing authority.
    """
    if media_item.processing_status != "processed":
        return
    hashes = dict(media_item.derivative_hashes_json or {})
    relevant = {media_item.preview_path, media_item.thumbnail_path} - {None, media_item.original_path}
    hashes = {path: digest for path, digest in hashes.items() if path in relevant}
    for stored in relevant:
        path = reserve_absolute(settings, stored)
        if stored not in hashes and path and path.is_file():
            hashes[stored] = path_sha256(path)
    media_item.derivative_hashes_json = hashes


def missing_media_paths(settings: Settings, media_item: MediaItem) -> dict[str, str]:
    paths = {
        "original_path": media_item.original_path,
        "thumbnail_path": media_item.thumbnail_path,
        "preview_path": media_item.preview_path,
        "hover_preview_path": media_item.hover_preview_path,
    }
    return {
        key: value
        for key, value in paths.items()
        if value and not stored_media_file_exists(settings, value)
    }


def repair_missing_media_references(
    session: Session,
    settings: Settings,
    limit: int | None = None,
) -> dict[str, Any]:
    statement = select(MediaItem).order_by(MediaItem.id)
    if limit:
        statement = statement.limit(limit)
    checked = repaired = missing_originals = queued_derivatives = 0
    repaired_media_item_ids: list[int] = []
    for media_item in session.scalars(statement).all():
        checked += 1
        missing = missing_media_paths(settings, media_item)
        if not missing:
            continue
        repaired += 1
        media_item.derivative_hashes_json = None
        repaired_media_item_ids.append(media_item.id)
        for field in ("thumbnail_path", "preview_path", "hover_preview_path"):
            if field in missing:
                setattr(media_item, field, None)
        if "original_path" in missing:
            missing_originals += 1
            media_item.original_path = None
            media_item.thumbnail_path = None
            media_item.preview_path = None
            media_item.hover_preview_path = None
            media_item.processing_status = "failed"
            media_item.processing_error = "Original file is missing"
            media_item.public_status = "draft"
            media_item.visibility = "private"
            media_item.public_original_allowed = False
            continue

        queued_derivatives += 1
        media_item.processing_status = "pending"
        media_item.processing_error = "Generated media file is missing; queued for regeneration"
    session.flush()
    return {
        "checked": checked,
        "repaired": repaired,
        "missing_originals": missing_originals,
        "queued_derivatives": queued_derivatives,
        "media_item_ids": repaired_media_item_ids,
    }


def _safe_stem(media_item: MediaItem) -> str:
    return f"media-{media_item.id:08d}"


def browser_playback_path(settings: Settings, media_item_id: int) -> Path:
    return settings.video_playback_dir / (
        f"media-{media_item_id:08d}-mobile-v{BROWSER_PLAYBACK_VERSION}.mp4"
    )


def ensure_browser_playback(settings: Settings, media_item_id: int, source: Path) -> Path:
    source = source.resolve()
    if not source.exists() or not source.is_file():
        raise RuntimeError("Original video is missing")

    output = browser_playback_path(settings, media_item_id)
    if _browser_playback_is_current(output, source):
        return output
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required to prepare browser video playback")

    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output.with_suffix(f"{output.suffix}.lock")
    with _exclusive_file_lock(lock_path):
        if _browser_playback_is_current(output, source):
            return output
        temporary = output.with_name(f".{output.stem}-{uuid4().hex}.tmp.mp4")
        transcode = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-sn",
            "-dn",
            "-vf",
            "scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-c:v",
            "libx264",
            "-profile:v",
            "baseline",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-tag:v",
            "avc1",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(temporary),
        ]
        try:
            _run_ffmpeg(transcode)
            if not temporary.exists() or temporary.stat().st_size <= 0:
                raise RuntimeError("Browser playback file was not generated")
            temporary.replace(output)
            return output
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Could not prepare browser-compatible video") from exc
        finally:
            temporary.unlink(missing_ok=True)


def _browser_playback_is_current(output: Path, source: Path) -> bool:
    return bool(
        output.exists()
        and output.is_file()
        and output.stat().st_size > 0
        and output.stat().st_mtime_ns >= source.stat().st_mtime_ns
    )


@contextmanager
def _exclusive_file_lock(path: Path, timeout_seconds: float = PLAYBACK_LOCK_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout_seconds
    with path.open("a+b") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Timed out waiting for browser playback preparation")
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def warm_browser_playback(settings: Settings, *, limit: int) -> dict[str, int]:
    limit = max(0, limit)
    report = {"checked": 0, "already_ready": 0, "generated": 0, "failed": 0}
    if limit == 0:
        return report
    with session_scope(settings) as session:
        candidates = list(
            session.execute(
                select(MediaItem.id, MediaItem.original_path)
                .where(
                    MediaItem.media_type == "video",
                    MediaItem.processing_status == "processed",
                    MediaItem.public_status == "published",
                    MediaItem.visibility == "public",
                    MediaItem.original_path.is_not(None),
                )
                .order_by(MediaItem.id.desc())
            )
        )

    attempted = 0
    for media_item_id, stored_path in candidates:
        source = reserve_absolute(settings, stored_path)
        report["checked"] += 1
        if source is None or not source.exists():
            continue
        output = browser_playback_path(settings, media_item_id)
        if _browser_playback_is_current(output, source):
            report["already_ready"] += 1
            continue
        if attempted >= limit:
            break
        attempted += 1
        try:
            ensure_browser_playback(settings, media_item_id, source)
            report["generated"] += 1
        except (OSError, RuntimeError):
            report["failed"] += 1
    return report


def _image_preview(
    source: Path,
    thumbnail: Path,
    preview: Path,
    thumbnail_size: tuple[int, int] = (360, 360),
    preview_size: tuple[int, int] = (1280, 1280),
) -> tuple[int, int, str]:
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)
        width, height = image.size
        preview_image = image.convert("RGB")
        thumb_image = preview_image.copy()
        thumb_image.thumbnail(thumbnail_size)
        preview_image.thumbnail(preview_size)
        thumbnail.parent.mkdir(parents=True, exist_ok=True)
        preview.parent.mkdir(parents=True, exist_ok=True)
        thumb_image.save(thumbnail, "JPEG", quality=82, optimize=True)
        preview_image.save(preview, "JPEG", quality=88, optimize=True)
        phash = str(imagehash.phash(preview_image))
    return width, height, phash


def _run_ffmpeg(args: list[str]) -> None:
    subprocess.run(
        args,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=FFMPEG_TIMEOUT_SECONDS,
    )


def _run_ffprobe(args: list[str]) -> dict[str, object]:
    result = subprocess.run(
        args,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=FFPROBE_TIMEOUT_SECONDS,
    )
    loaded = json.loads(result.stdout or "{}")
    return loaded if isinstance(loaded, dict) else {}


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "N/A":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "N/A":
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _video_metadata(source: Path) -> tuple[int | None, int | None, float | None]:
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required to inspect video metadata")

    payload = _run_ffprobe(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,duration:format=duration",
            "-of",
            "json",
            str(source),
        ]
    )
    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    stream = streams[0] if streams and isinstance(streams[0], dict) else {}
    format_info = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration = _optional_float(stream.get("duration")) or _optional_float(format_info.get("duration"))
    return _optional_int(stream.get("width")), _optional_int(stream.get("height")), duration


def _preview_start_time(duration_seconds: float | None) -> float:
    if not duration_seconds or duration_seconds <= 3.5:
        return 0.0
    latest_start = max(0.0, duration_seconds - 3.0)
    return min(max(0.5, duration_seconds * 0.25), latest_start)


def _video_hover_preview(
    settings: Settings,
    media_item: MediaItem,
    source: Path,
    duration_seconds: float | None,
) -> Path | None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required to generate video hover previews")

    output = settings.video_previews_dir / f"{_safe_stem(media_item)}.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    start_time = _preview_start_time(duration_seconds)
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start_time:.3f}",
            "-i",
            str(source),
            "-t",
            "3",
            "-an",
            "-vf",
            "fps=12,scale='min(480,iw)':-2,format=yuv420p",
            "-movflags",
            "+faststart",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            str(output),
        ]
    )
    return output if output.exists() and output.stat().st_size > 0 else None


def _video_keyframe_timestamps(count: int, duration_seconds: float | None) -> list[float]:
    if count <= 0:
        return []
    if not duration_seconds or duration_seconds <= 0:
        return [float(index * 2) for index in range(count)]
    if count == 1:
        return [max(0.0, duration_seconds * 0.5)]
    step = duration_seconds / float(count + 1)
    return [round(step * (index + 1), 3) for index in range(count)]


def _video_keyframes(
    settings: Settings,
    media_item: MediaItem,
    source: Path,
) -> tuple[Path | None, list[tuple[Path, float]]]:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required to generate video thumbnails and keyframes")

    stem = _safe_stem(media_item)
    preview = settings.previews_dir / f"{stem}.jpg"
    keyframe_dir = settings.keyframes_dir / stem
    keyframe_dir.mkdir(parents=True, exist_ok=True)

    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-vf",
            "thumbnail,scale='min(1280,iw)':-2",
            "-frames:v",
            "1",
            str(preview),
        ]
    )

    keyframes: list[tuple[Path, float]] = []
    for index, timestamp in enumerate(
        _video_keyframe_timestamps(settings.max_video_keyframes, media_item.duration_seconds)
    ):
        output = keyframe_dir / f"frame-{index:03d}.jpg"
        try:
            _run_ffmpeg(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale='min(1280,iw)':-2",
                    str(output),
                ]
            )
            if output.exists():
                keyframes.append((output, timestamp))
        except subprocess.CalledProcessError:
            continue
    return preview if preview.exists() else None, keyframes


def import_local_media_file(
    session: Session,
    settings: Settings,
    telegram_message_id: int,
    source: Path,
    mime_type: str | None = None,
) -> MediaItem:
    source = source.resolve()
    target_dir = settings.originals_dir / f"{telegram_message_id}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    if source != target:
        shutil.copy2(source, target)

    item = MediaItem(
        telegram_message_id=telegram_message_id,
        media_type=infer_media_type(target, mime_type),
        mime_type=mime_type or mimetypes.guess_type(target.name)[0],
        original_path=reserve_relative(settings, target),
        processing_status="pending",
        size_bytes=target.stat().st_size,
        sha256=path_sha256(target),
    )
    session.add(item)
    session.flush()
    return item


def process_media_item(session: Session, settings: Settings, media_item: MediaItem) -> bool:
    media_item.derivative_hashes_json = None
    source = reserve_absolute(settings, media_item.original_path)
    if source is None or not source.exists():
        media_item.processing_status = "failed"
        media_item.processing_error = "Original file is missing"
        return False

    try:
        digest, size = path_sha256(source), source.stat().st_size
        if media_item.origin == "folder" and (digest, size) != (media_item.sha256, media_item.size_bytes):
            raise ValueError("Folder original differs from imported snapshot")
        media_item.sha256 = digest
        media_item.size_bytes = size
        media_item.preview_path = None
        media_item.thumbnail_path = None
        stem = _safe_stem(media_item)

        if media_item.media_type in {"image", "gif", "sticker"}:
            thumbnail = settings.thumbnails_dir / f"{stem}.jpg"
            preview = settings.previews_dir / f"{stem}.jpg"
            width, height, phash = _image_preview(source, thumbnail, preview)
            media_item.width = width
            media_item.height = height
            media_item.thumbnail_path = reserve_relative(settings, thumbnail)
            media_item.preview_path = reserve_relative(settings, preview)
            media_item.hover_preview_path = None
            media_item.perceptual_hash = phash
        elif media_item.media_type == "video":
            width, height, duration_seconds = _video_metadata(source)
            media_item.width = width
            media_item.height = height
            media_item.duration_seconds = duration_seconds
            preview, keyframes = _video_keyframes(settings, media_item, source)
            if preview:
                media_item.preview_path = reserve_relative(settings, preview)
                try:
                    thumbnail = settings.thumbnails_dir / f"{stem}.jpg"
                    _, _, phash = _image_preview(
                        preview,
                        thumbnail,
                        settings.previews_dir / f"{stem}.jpg",
                    )
                    media_item.thumbnail_path = reserve_relative(settings, thumbnail)
                    media_item.perceptual_hash = phash
                except UnidentifiedImageError:
                    media_item.thumbnail_path = media_item.preview_path
            try:
                hover_preview = _video_hover_preview(settings, media_item, source, duration_seconds)
                media_item.hover_preview_path = reserve_relative(settings, hover_preview) if hover_preview else None
            except (RuntimeError, subprocess.CalledProcessError):
                media_item.hover_preview_path = None
            try:
                ensure_browser_playback(settings, media_item.id, source)
            except (OSError, RuntimeError):
                # Playback can still be prepared on demand, or the original served as a fallback.
                pass
            session.execute(delete(MediaKeyframe).where(MediaKeyframe.media_item_id == media_item.id))
            for index, (frame_path, timestamp) in enumerate(keyframes):
                session.add(
                    MediaKeyframe(
                        media_item_id=media_item.id,
                        frame_index=index,
                        timestamp_seconds=timestamp,
                        path=reserve_relative(settings, frame_path),
                        sha256=path_sha256(frame_path),
                    )
                )
        else:
            media_item.preview_path = media_item.original_path
            media_item.thumbnail_path = media_item.original_path
            media_item.hover_preview_path = None

        media_item.duplicate_group_id = duplicate_group_for(media_item)
        media_item.processing_status = "processed"
        establish_derivative_hashes(settings, media_item)
        media_item.processing_error = None
        return True
    except Exception as exc:  # noqa: BLE001 - pipeline must mark corrupt media and keep moving.
        media_item.processing_status = "failed"
        media_item.processing_error = str(exc)
        return False


def media_needs_processing(media_item: MediaItem) -> bool:
    if media_item.processing_status in {"pending", "failed"}:
        return True
    if media_item.media_type == "video":
        return not bool(
            media_item.thumbnail_path
            and media_item.preview_path
            and media_item.hover_preview_path
            and media_item.width
            and media_item.height
            and media_item.duration_seconds is not None
        )
    if media_item.media_type in {"image", "gif", "sticker"}:
        return not bool(media_item.thumbnail_path and media_item.preview_path)
    return False


def video_keyframes_need_backfill(session: Session, settings: Settings, media_item: MediaItem) -> bool:
    if media_item.media_type != "video" or settings.max_video_keyframes <= 0:
        return False
    keyframe_count = session.scalar(
        select(func.count()).select_from(MediaKeyframe).where(MediaKeyframe.media_item_id == media_item.id)
    )
    return int(keyframe_count or 0) < settings.max_video_keyframes


def duplicate_group_for(media_item: MediaItem) -> str | None:
    if media_item.sha256:
        return f"sha256:{media_item.sha256[:16]}"
    if media_item.perceptual_hash:
        return f"phash:{media_item.perceptual_hash}"
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


def _processing_description(session: Session, media_item: MediaItem) -> str:
    message = session.get(TelegramMessage, media_item.telegram_message_id)
    date = message.date.date().isoformat() if message and message.date else "unknown date"
    return f"Processing {date} media={media_item.id} type={media_item.media_type}"


def _processing_failure_context(session: Session, media_item: MediaItem) -> dict[str, object]:
    message = session.get(TelegramMessage, media_item.telegram_message_id)
    link_preview = message_link_preview(message)
    urls = [
        str(value)
        for key in ("url", "display_url", "embed_url")
        if (value := link_preview.get(key))
    ]
    return {
        "media_item_id": media_item.id,
        "telegram_message_id": message.telegram_message_id if message else None,
        "date": message.date.isoformat() if message and message.date else None,
        "original_path": media_item.original_path,
        "source_urls": urls,
        "processing_error": media_item.processing_error,
    }


def process_pending_media(session: Session, settings: Settings, limit: int | None = None) -> dict[str, object]:
    keyframe_count = (
        select(func.count(MediaKeyframe.id))
        .where(MediaKeyframe.media_item_id == MediaItem.id)
        .scalar_subquery()
    )
    statement = (
        select(MediaItem)
        .join(TelegramMessage)
        .where(
            (
                MediaItem.processing_status.in_(["pending", "failed"])
                | (
                    MediaItem.media_type.in_(["image", "gif", "sticker"])
                    & ((MediaItem.thumbnail_path.is_(None)) | (MediaItem.preview_path.is_(None)))
                )
                | (
                    (MediaItem.media_type == "video")
                    & (
                        (MediaItem.thumbnail_path.is_(None))
                        | (MediaItem.preview_path.is_(None))
                        | (MediaItem.hover_preview_path.is_(None))
                        | (MediaItem.width.is_(None))
                        | (MediaItem.height.is_(None))
                        | (MediaItem.duration_seconds.is_(None))
                        | (keyframe_count < settings.max_video_keyframes)
                    )
                )
            )
        )
        .order_by(TelegramMessage.date.desc(), TelegramMessage.telegram_message_id.desc(), MediaItem.id.desc())
    )
    if limit:
        statement = statement.limit(limit)
    media_items = session.scalars(statement).all()
    counts: dict[str, object] = {
        "processed": 0,
        "failed": 0,
        "total_candidates": len(media_items),
        "failed_items": [],
    }
    with _progress() as progress:
        task_id = progress.add_task("Processing media", total=len(media_items))
        for media_item in media_items:
            progress.update(task_id, description=_processing_description(session, media_item))
            ok = process_media_item(session, settings, media_item)
            if ok:
                counts["processed"] = int(counts["processed"]) + 1
            else:
                counts["failed"] = int(counts["failed"]) + 1
                failed_items = counts["failed_items"]
                if isinstance(failed_items, list):
                    failed_items.append(_processing_failure_context(session, media_item))
            progress.advance(task_id)
    session.flush()
    return counts
