"""Internal single-record intake. See docs/source-ingestion.md for its limits."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import mimetypes
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .media import infer_media_type, path_sha256
from .models import MediaItem, SourceIdentity, TelegramMessage, utc_now
from .owner import require_owner


@dataclass(frozen=True)
class SourceRecord:
    namespace: str
    external_key: str
    caption: str | None = None
    date: datetime | None = None
    kind: str = "folder"


@dataclass(frozen=True)
class IngestResult:
    status: Literal["created", "unchanged", "conflict"]
    media_item_id: int
    message_id: int

    @property
    def media_item_ids(self) -> list[int]:
        return [self.media_item_id] if self.status == "created" else []


class CommitUncertain(RuntimeError):
    """Commit raised; retain attempt files and resolve identity on a later retry."""


def _source(root: Path, record: SourceRecord) -> Path:
    if record.kind != "folder":
        raise ValueError("Only folder sources are supported")
    for value, limit in ((record.namespace, 128), (record.external_key, 1024)):
        if not value or "\0" in value or len(value.encode("utf-8")) > limit:
            raise ValueError("Invalid source key or UTF-8 byte limit exceeded")
    parts = record.external_key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("Expected an exact POSIX relative key")
    if record.date is not None and record.date.utcoffset() is None:
        raise ValueError("Source date must be timezone aware")
    path = root.resolve(strict=True)
    if not path.is_dir():
        raise ValueError("Source root must be a directory")
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("Source symlinks are unsupported")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Source must be a regular file")
    return path


def _signature(path: Path) -> tuple[int, ...]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _snapshot(source: Path, target: Path) -> tuple[str, int]:
    before = _signature(source)
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as reader, target.open("xb") as writer:
        for block in iter(lambda: reader.read(1024 * 1024), b""):
            writer.write(block)
            digest.update(block)
            size += len(block)
    if before != _signature(source) or size != before[2]:
        raise ValueError("Source changed during intake")
    return digest.hexdigest(), size


def _existing(db: Session, settings: Settings, record: SourceRecord, owner_id: str | None,
              digest: str, size: int) -> IngestResult | None:
    mapping = db.scalar(select(SourceIdentity).where(
        SourceIdentity.kind == record.kind, SourceIdentity.namespace == record.namespace,
        SourceIdentity.external_key == record.external_key))
    if mapping is None:
        return None
    if mapping.owner_id != owner_id:
        raise ValueError("Source ownership mismatch; adoption is not supported")
    media = db.get(MediaItem, mapping.media_item_id)
    if media is None or db.get(TelegramMessage, media.telegram_message_id) is None:
        raise ValueError("Incomplete mapped source")
    if media.public_status == "deleted" or not media.original_path or not media.sha256:
        raise ValueError("Mapped source is deleted or incomplete")
    reserve = settings.reserve_path.resolve()
    stored = (reserve / media.original_path).resolve()
    if not stored.is_relative_to(reserve) or not stored.is_file():
        raise ValueError("Mapped reserve original missing or outside reserve")
    if stored.stat().st_size != media.size_bytes or path_sha256(stored) != media.sha256:
        raise ValueError("Mapped reserve original corrupt")
    status = "unchanged" if (digest, size) == (media.sha256, media.size_bytes) else "conflict"
    return IngestResult(status, media.id, media.telegram_message_id)


def _source_race(error: IntegrityError) -> bool:
    original = error.orig
    if getattr(getattr(original, "diag", None), "constraint_name", None) == "uq_source_identity":
        return True
    return str(original) == (
        "UNIQUE constraint failed: source_identities.kind, source_identities.namespace, "
        "source_identities.external_key")


def ingest_local_record(factory: sessionmaker, settings: Settings, root: Path,
                        record: SourceRecord, *, mode: Literal["local", "hosted"],
                        owner_id: str | None = None) -> IngestResult:
    """Own a fresh session per attempt; return created IDs only after commit."""
    source = _source(root, record)
    if mode not in ("local", "hosted") or (mode == "local" and owner_id is not None):
        raise ValueError("Explicit local mode requires no owner")
    reserve = settings.reserve_path.resolve(strict=True)
    for _ in range(3):
        attempt = None
        uncertain = False
        try:
            with factory() as db:
                if mode == "hosted" and (owner_id is None or require_owner(settings, db) != owner_id):
                    raise ValueError("Hosted intake requires the configured provisioned owner")
                # An exclusive directory makes every file removable by this attempt alone.
                parent = reserve / "originals" / "folder"
                parent.mkdir(parents=True, exist_ok=True)
                if not parent.resolve().is_relative_to(reserve):
                    raise ValueError("Intake destination escapes reserve")
                attempt = Path(tempfile.mkdtemp(prefix="attempt-", dir=parent))
                temporary = attempt / "snapshot.tmp"
                digest, size = _snapshot(source, temporary)
                existing = _existing(db, settings, record, owner_id, digest, size)
                if existing is not None:
                    return existing
                final = attempt / ("original" + source.suffix)
                temporary.rename(final)
                message = TelegramMessage(telegram_message_id=1, chat_id=f"folder-import:{uuid4().hex}",
                                          caption=record.caption, date=record.date, raw_metadata_json={})
                db.add(message)
                db.flush()
                media = MediaItem(telegram_message_id=message.id, media_type=infer_media_type(final),
                                  mime_type=mimetypes.guess_type(final.name)[0],
                                  original_path=final.relative_to(reserve).as_posix(), sha256=digest,
                                  size_bytes=size, processing_status="pending", origin="folder",
                                  public_status="published", visibility="public",
                                  public_original_allowed=True, published_at=utc_now())
                db.add(media)
                db.flush()
                db.add(SourceIdentity(kind=record.kind, namespace=record.namespace,
                                      external_key=record.external_key, owner_id=owner_id,
                                      media_item_id=media.id))
                db.flush()
                result = IngestResult("created", media.id, message.id)
                # Even cancellation can arrive after the database commits. From
                # this point cleanup must retain the file unless success is known.
                uncertain = True
                try:
                    db.commit()
                except Exception as error:
                    raise CommitUncertain(f"Commit outcome uncertain; retained {attempt}") from error
                attempt = None  # committed originals belong to the reserve now
                return result
        except IntegrityError as error:
            if not _source_race(error):
                raise
        finally:
            if attempt is not None and not uncertain:
                shutil.rmtree(attempt)
    raise RuntimeError("Source identity race did not converge after three attempts")
