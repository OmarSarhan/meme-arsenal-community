"""Explicit private storage authority; no cookie or public request fallback."""
from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from pathlib import Path

from .config import Settings, validate_owner_id
from .db import get_engine
from .media import media_item_has_original_file
from .models import AnonymousSession, AnonymousSessionCuration, Curation, MediaItem, RemixJobRecord, TelegramMessage


def require_owner(settings: Settings, db: Session | None = None) -> str:
    owner = validate_owner_id(settings.owner_session_id)
    if db is not None:
        if db.get(AnonymousSession, owner) is None:
            raise ValueError("Configured owner row missing; run offline owner inventory and adopt/initialize")
    else:
        engine = get_engine(settings)
        try:
            with Session(engine) as session:
                require_owner(settings, session)
        finally:
            engine.dispose()
    return owner


def source_eligible(db: Session, settings: Settings, item: MediaItem | None, owner: str | None) -> bool:
    if item is None or item.processing_status != "processed" or not media_item_has_original_file(settings, item):
        return False
    if item.public_status == "published" and item.visibility == "public":
        return True
    if owner and item.origin == "dashboard_upload":
        message = db.get(TelegramMessage, item.telegram_message_id)
        return bool(message and message.chat_id == f"dashboard-upload:{owner}")
    return False


def inventory(settings: Settings) -> dict:
    if settings.is_sqlite and not Path(make_url(settings.database_url).database).is_file():
        raise ValueError("Database missing; migrate first")
    engine = get_engine(settings)
    try:
        with engine.connect() as connection, connection.begin():
            connection.execute(text("PRAGMA query_only = ON" if settings.is_sqlite else "SET TRANSACTION READ ONLY"))
            with Session(bind=connection) as db:
                def count(model, *conditions):
                    return db.scalar(select(func.count()).select_from(model).where(*conditions))
                rows = []
                for row in db.scalars(select(AnonymousSession).order_by(AnonymousSession.id)):
                    try:
                        validate_owner_id(row.id)
                        adoptable = True
                    except ValueError:
                        adoptable = False
                    uploads = select(MediaItem.id).join(TelegramMessage).where(
                        TelegramMessage.chat_id == f"dashboard-upload:{row.id}")
                    rows.append(dict(id=row.id, adoptable=adoptable, created_at=str(row.created_at),
                                     last_seen_at=str(row.last_seen_at), scope="selected" if row.id == settings.owner_session_id else "excluded",
                                     curation=count(AnonymousSessionCuration, AnonymousSessionCuration.session_id == row.id),
                                     uploads=len(db.scalars(uploads).all()),
                                     saved_uploads=len(db.scalars(uploads.where(MediaItem.public_status == "published")).all()),
                                     jobs=dict(db.execute(select(RemixJobRecord.status, func.count()).where(
                                         RemixJobRecord.session_id == row.id).group_by(RemixJobRecord.status)).all())))
                return dict(namespaces=rows, null_jobs=count(RemixJobRecord, RemixJobRecord.session_id.is_(None)),
                            global_curation=count(Curation), owner_selection="configured" if settings.owner_session_id else "unavailable")
    finally:
        engine.dispose()


def provision(settings: Settings, *, initialize: bool, allow_empty_namespace: bool = False) -> str:
    owner = validate_owner_id(settings.owner_session_id)
    engine = get_engine(settings)
    try:
        with Session(engine) as db, db.begin():
            if db.get(AnonymousSession, owner) is not None:
                return "Verified existing selected namespace"
            if not initialize:
                raise ValueError("Selected namespace does not exist; inventory before adoption")
            if not allow_empty_namespace and (db.scalar(select(AnonymousSession.id).limit(1)) is not None
                                               or db.scalar(select(MediaItem.id).limit(1)) is not None):
                raise ValueError("Existing library: inventory first; explicit --allow-empty-namespace required")
            db.add(AnonymousSession(id=owner))
        return "Initialized selected namespace; other records preserved"
    except IntegrityError:
        require_owner(settings)
        return "Verified existing selected namespace"
    finally:
        engine.dispose()
