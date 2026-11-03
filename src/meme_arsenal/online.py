from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timedelta, timezone
import json
import base64
import html
import hashlib
import hmac
import ipaddress
import os
from pathlib import Path
import random
import re
import secrets
import shutil
import uuid
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, delete, func, literal_column, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased, joinedload, selectinload, sessionmaker
from starlette.convertors import Convertor, register_url_convertor

from .ai import ClassificationPayload, classify_media_item, classification_block_reason, classification_status
from .result_identity import generation_identity, saved_result_identity
from .catalog import _normalize_language, catalog_saved_at
from .media import (
    BROWSER_PLAYBACK_VERSION,
    ensure_browser_playback,
    import_local_media_file,
    media_item_has_original_file,
    process_media_item,
    reserve_absolute,
    stored_media_file_exists,
)
from .config import Settings, ensure_reserve_layout, load_settings
from .db import get_engine
from .owner import require_owner, source_eligible
from .link_preview import message_link_preview
from .google_api import google_usage_context
from .playback_diagnostics import PlaybackDiagnostic, log_playback_diagnostic
from .models import (
    AnonymousSession,
    AnonymousSessionCuration,
    Classification,
    MediaItem,
    MediaKeyframe,
    MediaTag,
    PublicMediaShare,
    PublicShareCode,
    PublicSearchDocument,
    RemixJobRecord,
    RemixSourceRecord,
    TelegramMessage,
)
from .remix import RemixResult, analyze_remix_tweaks, remix_unavailable_reason, remix_tweaks_from_payload, save_remix_result, remix_safety_mode_from_payload
from .remix_execution import RemixGenerationProvenance, capture_execution
from copy import deepcopy
from .remix_card import render_remix_card
from .remix_page import render_remix_page


PUBLIC_STATIC_ROOTS = (
    "media/originals/",
    "media/thumbnails/",
    "media/previews/",
    "media/video-previews/",
    "media/keyframes/",
)
TAG_FIELDS = ("topics", "keywords", "tone", "reaction_type", "manual_tags")
SINGLE_TAG_FIELDS = ("meme_format", "media_type", "language", "duplicate_group")
UPLOAD_CHAT_PREFIX = "dashboard-upload"
DASHBOARD_UPLOAD_ORIGIN = "dashboard_upload"
TAG_VALUES_PER_GROUP = 100
PUBLIC_TAG_GROUPS = ("topics", "tone", "reaction_type", "meme_format", "media_type", "language")
_ITEM_PAYLOAD_UNSET = object()
SHARE_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ23456789"
SHARE_CODE_PATTERN = r"[A-Z2-9]{5}"


class ShareCodeConvertor(Convertor[str]):
    regex = SHARE_CODE_PATTERN

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("sharecode", ShareCodeConvertor())

UPLOAD_CHUNK_BYTES = 1024 * 1024
REMIX_UPLOAD_EXTENSIONS = {
    ".avi",
    ".bmp",
    ".gif",
    ".gifv",
    ".heic",
    ".jpeg",
    ".jpg",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".png",
    ".webm",
    ".webp",
}


class CurationPatch(BaseModel):
    favorite: bool | None = None
    archived: bool | None = None
    manual_tags: list[str] | None = None
    notes: str | None = None


class MetadataPatch(BaseModel):
    summary: str = Field(max_length=4000)
    visible_text: str = Field(default="", max_length=16000)
    narration: str = Field(default="", max_length=16000)
    language: str = Field(default="unknown", max_length=64)
    meme_format: str = Field(default="unknown", max_length=128)
    keywords: list[str] = Field(default_factory=list, max_length=100)
    topics: list[str] = Field(default_factory=list, max_length=100)
    people_or_characters: list[str] = Field(default_factory=list, max_length=100)
    tone: list[str] = Field(default_factory=list, max_length=100)
    reaction_type: list[str] = Field(default_factory=list, max_length=100)


class RemixJobCreate(BaseModel):
    left_id: int
    right_id: int
    safety_mode: str = "standard"
    tweaks: dict[str, Any] | None = None


class ApiItemsResponse(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None = None
    total: int | None = None


class RuntimeState:
    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self.session_local = sessionmaker(bind=engine, future=True, expire_on_commit=False)
        self.signer = URLSafeSerializer(settings.public_session_secret, salt="meme-arsenal-public-session")
        self.share_signer = URLSafeSerializer(settings.public_session_secret, salt="meme-arsenal-public-share")


def _validate_online_settings(settings: Settings) -> None:
    if settings.public_share_url:
        label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        origin = re.fullmatch(rf"https://({label}(?:\.{label})+)(?::443)?/?", settings.public_share_url, re.I | re.ASCII)
        if not origin or len(origin[1]) > 253 or not re.search(r"[a-z]", origin[1].split(".")[-1], re.I | re.ASCII):
            raise RuntimeError("PUBLIC_SHARE_URL must be a separate HTTPS DNS origin on port 443")
        share_origin = urlsplit(settings.public_share_url)
        if (
            share_origin.scheme != "https"
            or not share_origin.hostname
            or share_origin.path not in ("", "/")
            or share_origin.query
            or share_origin.fragment
            or share_origin.username
            or share_origin.password
            or share_origin.port not in (None, 443)
            or share_origin.hostname == urlsplit(settings.public_site_url).hostname
        ):
            raise RuntimeError("PUBLIC_SHARE_URL must be a separate HTTPS origin without a path")
    if settings.app_env.strip().lower() != "production":
        return
    if settings.public_session_secret == "change-me-in-production" or len(settings.public_session_secret) < 32:
        raise RuntimeError("PUBLIC_SESSION_SECRET must be a non-default secret of at least 32 characters")
    if not settings.is_postgres:
        raise RuntimeError("Production online service requires PostgreSQL")
    if not settings.public_site_url.lower().startswith("https://"):
        raise RuntimeError("Production PUBLIC_SITE_URL must use HTTPS")
    if not settings.public_session_cookie_secure:
        raise RuntimeError("Production session cookies must be secure")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    _validate_online_settings(settings)
    require_owner(settings)
    ensure_reserve_layout(settings)
    engine = get_engine(settings)

    app = FastAPI(title="Meme Arsenal Online", version="0.1.1")
    app.state.runtime = RuntimeState(settings, engine)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.public_site_url.rstrip("/")],
        allow_credentials=True,
        allow_methods=["DELETE", "GET", "PATCH", "POST"],
        allow_headers=["content-type"],
    )

    @app.middleware("http")
    async def isolate_public_share_host(request: Request, call_next):
        share_host = urlsplit(settings.public_share_url).hostname
        if share_host and request.url.hostname == share_host:
            short_path = re.fullmatch(
                rf"/{SHARE_CODE_PATTERN}(?:/(?:media|poster|events|card(?:/image)?))?",
                request.url.path,
            )
            method_allowed = (request.method == "GET" and not request.url.path.endswith("/events")
                              or request.method == "HEAD" and bool(re.fullmatch(rf"/{SHARE_CODE_PATTERN}(?:/(?:media|poster))?", request.url.path))
                              or request.method == "POST" and request.url.path.endswith("/events"))
            if not short_path or not method_allowed:
                return Response(status_code=404, headers=_public_share_media_headers())
        public_path = request.url.path.startswith("/share/") or bool(re.match(rf"^/{SHARE_CODE_PATTERN}(?:/|$)", request.url.path))
        if public_path and not settings.public_sharing_enabled:
            return Response(status_code=404, headers=_public_share_media_headers())
        response = await call_next(request)
        if public_path or request.url.path.endswith("/share"):
            response.headers.update(_public_share_media_headers())
        return response

    def limit_public_share(request: Request) -> None:
        _check_write_limit(request, _client_ip_hash(request), "share-read", 120, 60)

    @app.get("/api/health")
    def health(response: Response) -> dict[str, Any]:
        database_ok = False
        redis_ok = False
        try:
            with app.state.runtime.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            database_ok = True
        except Exception:
            database_ok = False
        try:
            import redis

            client = redis.Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            redis_ok = bool(client.ping())
        except Exception:
            redis_ok = False
        ok = database_ok and redis_ok
        if not ok:
            response.status_code = 503
        return {
            "ok": ok,
            "database": "postgresql" if settings.is_postgres else "sqlite",
            "database_ok": database_ok,
            "redis_url_configured": bool(settings.redis_url),
            "redis_ok": redis_ok,
        }

    @app.get("/api/remix/health")
    def remix_health() -> dict[str, Any]:
        reason = remix_unavailable_reason(settings)
        return {
            "enabled": reason is None,
            "provider": settings.effective_remix_provider,
            "model": settings.remix_model,
            "aspect_ratio": settings.remix_aspect_ratio,
            "image_size": settings.remix_image_size,
            "message": reason or "Ready",
        }

    @app.get("/api/items", response_model=ApiItemsResponse)
    def list_items(
        request: Request,
        response: Response,
        session_id: str = Depends(require_private_owner),
        db: Session = Depends(db_session),
        limit: int | None = Query(default=None, ge=1),
        cursor: str | None = Query(default=None, max_length=128),
        q: str = "",
        media_type: str = "all",
        topic: str = "all",
        tone: str = "all",
        reaction_type: str = "all",
        meme_format: str = "all",
        language: str = "all",
        favorite: bool = False,
        archived: bool = False,
        include_hidden: bool = False,
    ) -> ApiItemsResponse:
        runtime: RuntimeState = request.app.state.runtime
        page_limit = min(
            limit or runtime.settings.public_items_default_limit,
            runtime.settings.public_items_max_limit,
        )
        rows, next_cursor, total = _query_public_items(
            db,
            runtime.settings,
            session_id=session_id,
            limit=page_limit,
            cursor=cursor,
            q=q,
            media_type=media_type,
            topic=topic,
            tone=tone,
            reaction_type=reaction_type,
            meme_format=meme_format,
            language=language,
            favorite=favorite,
            archived=archived,
            include_hidden=include_hidden,
        )
        session_curation = _session_curation_map(db, session_id, [item.id for item in rows])
        remix_sources, remix_used_by = _remix_trace_maps(db, runtime.settings, [item.id for item in rows], session_id=session_id)
        items = [
            _item_payload(
                db,
                runtime.settings,
                media_item,
                session_id=session_id,
                session_curation=session_curation.get(media_item.id),
                remix_sources=remix_sources.get(media_item.id, []),
                remix_used_by=remix_used_by.get(media_item.id, []),
            )
            for media_item in rows
        ]
        _touch_session_cookie(request, response, session_id)
        return ApiItemsResponse(items=items, next_cursor=next_cursor, total=total)

    @app.get("/api/items/random")
    def random_item(
        request: Request,
        response: Response,
        session_id: str = Depends(require_private_owner),
        db: Session = Depends(db_session),
        q: str = "",
        media_type: str = "all",
        topic: str = "all",
        tone: str = "all",
        reaction_type: str = "all",
        meme_format: str = "all",
        language: str = "all",
        favorite: bool = False,
        archived: bool = False,
    ) -> dict[str, Any]:
        runtime: RuntimeState = request.app.state.runtime
        media_item = _query_random_public_item(
            db,
            runtime.settings,
            session_id=session_id,
            q=q,
            media_type=media_type,
            topic=topic,
            tone=tone,
            reaction_type=reaction_type,
            meme_format=meme_format,
            language=language,
            favorite=favorite,
            archived=archived,
        )
        if media_item is None:
            raise HTTPException(status_code=404, detail="Item not found")
        _touch_session_cookie(request, response, session_id)
        return _item_payload(db, runtime.settings, media_item, session_id=session_id)

    @app.get("/api/items/{media_item_id}")
    def get_item(
        media_item_id: int,
        request: Request,
        response: Response,
        session_id: str = Depends(require_private_owner),
        db: Session = Depends(db_session),
    ) -> dict[str, Any]:
        runtime: RuntimeState = request.app.state.runtime
        media_item = _public_media_item_or_404(db, runtime.settings, media_item_id)
        _touch_session_cookie(request, response, session_id)
        return _item_payload(db, runtime.settings, media_item, session_id=session_id)

    @app.get("/api/items/{media_item_id}/remix-card")
    def remix_card_image(
        media_item_id: int,
        request: Request,
        page: bool = False,
        source: Literal["A", "B"] | None = None,
        playback: bool = False,
        session_id: str = Depends(require_private_owner),
        db: Session = Depends(db_session),
    ) -> Response:
        runtime: RuntimeState = request.app.state.runtime
        item = _shareable_media_item_or_404(db, runtime.settings, media_item_id)
        base = f"/api/items/{item.id}/remix-card"
        return _remix_card_response(db, runtime.settings, item, page=page, source=source, playback=playback, image_base=base, private_owner=session_id)

    @app.get("/api/items/{media_item_id}/share")
    def get_public_share(
        media_item_id: int,
        request: Request,
        card: bool = False,
        db: Session = Depends(db_session),
    ) -> dict[str, Any]:
        runtime: RuntimeState = request.app.state.runtime
        _shareable_media_item_or_404(db, runtime.settings, media_item_id)
        share = db.scalar(
            select(PublicMediaShare).where(PublicMediaShare.media_item_id == media_item_id)
        )
        return _public_share_payload(db, runtime, share, card=card)

    @app.post("/api/items/{media_item_id}/share")
    def create_public_share(
        media_item_id: int,
        request: Request,
        card: bool = False,
        db: Session = Depends(db_session),
    ) -> dict[str, Any]:
        runtime: RuntimeState = request.app.state.runtime
        _require_same_origin(request, runtime.settings)
        if not runtime.settings.public_sharing_enabled:
            raise HTTPException(status_code=409, detail="sharing_disabled")
        item = _locked_shareable_item(db, runtime.settings, media_item_id)
        if card and item.origin != "remix":
            raise HTTPException(status_code=404, detail="Remix card not found")
        share = db.scalar(
            select(PublicMediaShare)
            .where(PublicMediaShare.media_item_id == media_item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        now = _utc_now()
        if share is None:
            share = PublicMediaShare(
                media_item_id=media_item_id,
                token_id=uuid.uuid4().hex,
                active=True,
                created_at=now,
                updated_at=now,
            )
            db.add(share)
        elif not share.active:
            share.token_id = uuid.uuid4().hex
            share.active = True
            share.created_at = now
            share.updated_at = now
            share.revoked_at = None
        db.flush()
        if card and share.recipe_grant_json is None:
            share.recipe_grant_json = _capture_recipe_grant(db, runtime.settings, item)
        if runtime.settings.public_share_url:
            _public_share_code(db, share, card=card)
        db.flush()
        return _public_share_payload(db, runtime, share, card=card)

    @app.delete("/api/items/{media_item_id}/share")
    def recall_public_share(
        media_item_id: int,
        request: Request,
        db: Session = Depends(db_session),
    ) -> dict[str, Any]:
        runtime: RuntimeState = request.app.state.runtime
        _require_same_origin(request, runtime.settings)
        _locked_shareable_item(db, runtime.settings, media_item_id, require_eligible=False)
        share = db.scalar(
            select(PublicMediaShare)
            .where(PublicMediaShare.media_item_id == media_item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if share is not None and share.active:
            now = _utc_now()
            share.active = False
            share.recipe_grant_json = None
            share.revoked_at = now
            share.updated_at = now
            db.flush()
        return _public_share_payload(db, runtime, share)

    @app.get("/api/items/{media_item_id}/playback")
    @app.head("/api/items/{media_item_id}/playback")
    @app.get(f"/api/items/{{media_item_id}}/playback/mobile-v{BROWSER_PLAYBACK_VERSION}")
    @app.head(f"/api/items/{{media_item_id}}/playback/mobile-v{BROWSER_PLAYBACK_VERSION}")
    def item_playback(media_item_id: int, request: Request) -> FileResponse:
        runtime: RuntimeState = request.app.state.runtime
        session_id = require_owner(runtime.settings)
        with runtime.session_local() as db:
            media_item = _playback_media_item_or_404(
                db,
                runtime.settings,
                media_item_id,
                session_id=session_id,
            )
            source = reserve_absolute(runtime.settings, media_item.original_path)
        if source is None:
            raise HTTPException(status_code=404, detail="Video not found")
        headers = {"Cache-Control": "private, no-store"}
        try:
            playback = ensure_browser_playback(runtime.settings, media_item_id, source)
            return FileResponse(playback, media_type="video/mp4", headers=headers)
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503,
                detail="Browser-compatible playback is still being prepared",
                headers={"Retry-After": "10"},
            ) from exc

    @app.post("/api/items/{media_item_id}/playback-events", status_code=204)
    def playback_event(
        media_item_id: int,
        payload: PlaybackDiagnostic,
        request: Request,
        session_id: str = Depends(require_private_owner),
        db: Session = Depends(db_session),
    ) -> Response:
        _require_same_origin(request, settings)
        _check_write_limit(request, session_id, "playback", 120, 60)
        _playback_media_item_or_404(db, settings, media_item_id, session_id=session_id)
        log_playback_diagnostic(media_item_id, request.headers.get("user-agent", ""), payload)
        return Response(status_code=204)

    @app.get("/api/tags")
    def tags(db: Session = Depends(db_session)) -> dict[str, list[dict[str, Any]]]:
        return _tags_payload(db)

    @app.get("/api/catalog-state")
    def catalog_state(request: Request, db: Session = Depends(db_session)) -> dict[str, Any]:
        latest_index_update = db.scalar(select(func.max(PublicSearchDocument.updated_at)))
        latest_media_update = db.scalar(select(func.max(MediaItem.updated_at)))
        latest_item_id = db.scalar(select(func.max(MediaItem.id)))
        return {
            "item_count": db.scalar(select(func.count()).select_from(_public_media_statement().subquery())) or 0,
            "latest_item_id": latest_item_id,
            "updated_at": (
                max(value for value in [latest_index_update, latest_media_update] if value is not None).isoformat()
                if latest_index_update or latest_media_update
                else None
            ),
        }

    @app.patch("/api/session/items/{media_item_id}")
    def patch_session_curation(
        media_item_id: int,
        patch: CurationPatch,
        request: Request,
        response: Response,
        session_id: str = Depends(require_private_owner),
        db: Session = Depends(db_session),
    ) -> dict[str, Any]:
        runtime: RuntimeState = request.app.state.runtime
        _check_write_limit(request, session_id, "curation", runtime.settings.public_write_actions_per_minute, 60)
        media_item = _public_media_item_or_404(db, runtime.settings, media_item_id)
        curation = _session_curation(db, session_id, media_item_id, create=True)
        if patch.favorite is not None:
            curation.favorite = patch.favorite
        if patch.archived is not None:
            curation.archived = patch.archived
        if patch.manual_tags is not None:
            curation.manual_tags_json = _clean_tags(patch.manual_tags)
        if patch.notes is not None:
            curation.notes = patch.notes[:4000]
        curation.updated_at = _utc_now()
        db.flush()
        _touch_session_cookie(request, response, session_id)
        return _item_payload(db, runtime.settings, media_item, session_id=session_id)

    @app.patch("/api/items/{media_item_id}/metadata")
    def edit_item_metadata(
        media_item_id: int,
        patch: MetadataPatch,
        request: Request,
        session_id: str = Depends(require_private_owner),
        db: Session = Depends(db_session),
    ) -> dict[str, Any]:
        runtime: RuntimeState = request.app.state.runtime
        _require_same_origin(request, runtime.settings)
        _check_write_limit(request, session_id, "metadata", runtime.settings.public_write_actions_per_minute, 60)
        item = db.scalar(_public_media_statement().where(MediaItem.id == media_item_id).with_for_update())
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")
        previous = _latest_classification(db, item.id)
        manual = db.scalar(select(Classification).where(
            Classification.media_item_id == item.id, Classification.model == "manual",
            Classification.prompt_version == "manual-v1",
        ))
        if manual is None:
            manual = Classification(media_item_id=item.id, model="manual", prompt_version="manual-v1")
            db.add(manual)
        for field in ("summary", "visible_text", "narration", "language", "meme_format"):
            setattr(manual, field, getattr(patch, field).strip())
        for field in ("keywords", "topics", "people_or_characters", "tone", "reaction_type"):
            column = field if field in {"tone", "reaction_type"} else f"{field}_json"
            setattr(manual, column, _clean_tags(getattr(patch, field)))
        manual.safety_flags_json = previous.safety_flags_json if previous else []
        manual.search_phrases_json = []
        manual.raw_response_json = {"provider": "manual", "classification_status": "ok", **patch.model_dump()}
        manual.created_at = _utc_now()
        item.updated_at = _utc_now()
        db.flush()
        db.expire(item, ["classifications"])
        refresh_public_indexes(db, [item.id], settings=runtime.settings)
        return _item_payload(db, runtime.settings, item, session_id=session_id)

    @app.post("/api/items/{media_item_id}/classification/retry")
    def retry_item_classification(
        media_item_id: int,
        request: Request,
        response: Response,
        session_id: str = Depends(require_private_owner),
        db: Session = Depends(db_session),
    ) -> dict[str, Any]:
        """Retry a classifier-blocked item on an explicit user action only."""
        runtime: RuntimeState = request.app.state.runtime
        _require_same_origin(request, runtime.settings)
        _check_write_limit(request, session_id, "classification", runtime.settings.public_write_actions_per_minute, 60)
        media_item = _public_media_item_or_404(db, runtime.settings, media_item_id)
        try:
            with google_usage_context(operation="classification_retry", media_item_id=media_item.id):
                classify_media_item(db, runtime.settings, media_item, force=True)
        except Exception as exc:  # The original blocked result remains useful if the retry cannot run.
            raise HTTPException(status_code=503, detail=f"Could not retry classification: {exc}") from exc
        refresh_public_indexes(db, [media_item.id], settings=runtime.settings)
        db.flush()
        _touch_session_cookie(request, response, session_id)
        return _item_payload(db, runtime.settings, media_item, session_id=session_id)

    @app.post("/api/remix/jobs", status_code=202)
    def create_remix_job(
        payload: RemixJobCreate,
        request: Request,
        response: Response,
        session_id: str = Depends(require_private_owner),
        db: Session = Depends(db_session),
    ) -> dict[str, Any]:
        runtime: RuntimeState = request.app.state.runtime
        _check_write_limit(
            request,
            session_id,
            "remix",
            runtime.settings.public_remix_jobs_per_hour,
            60 * 60,
        )
        if payload.left_id == payload.right_id:
            raise HTTPException(status_code=400, detail="Choose two different memes to remix")
        _remix_media_item_or_404(db, runtime.settings, payload.left_id, session_id=session_id)
        _remix_media_item_or_404(db, runtime.settings, payload.right_id, session_id=session_id)
        if reason := remix_unavailable_reason(runtime.settings):
            raise HTTPException(status_code=503, detail=reason)
        _lock_anonymous_session(db, session_id)
        active_jobs = db.scalar(
            select(func.count())
            .select_from(RemixJobRecord)
            .where(
                RemixJobRecord.session_id == session_id,
                RemixJobRecord.status.in_(["queued", "preparing_media", "calling_gemini", "saving"]),
            )
        )
        if active_jobs and active_jobs >= runtime.settings.public_max_active_remix_jobs:
            raise HTTPException(status_code=429, detail="Too many active remix jobs")
        try:
            capture = capture_execution(runtime.settings, "new_job")
            tweaks = remix_tweaks_from_payload(payload.tweaks)
            safety_mode = remix_safety_mode_from_payload(payload.safety_mode)
        except (ValueError, TypeError):
            raise HTTPException(status_code=503, detail="Remix configuration is incompatible. Create a new job.") from None
        job = RemixJobRecord(
            id=uuid.uuid4().hex,
            session_id=session_id,
            left_id=payload.left_id,
            right_id=payload.right_id,
            safety_mode=safety_mode,
            tweaks_json=deepcopy(tweaks.payload()) if tweaks else {},
            execution_capture_json=capture.payload(),
            execution_first_captured_attempt=1,
            status="queued",
            message="Queued",
            result_json={},
            created_at=_utc_now(),
            updated_at=_utc_now(),
        )
        db.add(job)
        db.flush()
        enqueue_remix_job(runtime.settings, job.id)
        _touch_session_cookie(request, response, session_id)
        return _job_payload(job)

    @app.post("/api/remix/analysis")
    def remix_analysis(
        payload: RemixJobCreate,
        request: Request,
        response: Response,
        session_id: str = Depends(require_private_owner),
        db: Session = Depends(db_session),
    ) -> dict[str, Any]:
        runtime: RuntimeState = request.app.state.runtime
        _check_write_limit(
            request,
            session_id,
            "analysis",
            runtime.settings.public_remix_analyses_per_hour,
            60 * 60,
        )
        if payload.left_id == payload.right_id:
            raise HTTPException(status_code=400, detail="Choose two different memes to remix")
        _remix_media_item_or_404(db, runtime.settings, payload.left_id, session_id=session_id)
        _remix_media_item_or_404(db, runtime.settings, payload.right_id, session_id=session_id)
        if reason := remix_unavailable_reason(runtime.settings):
            raise HTTPException(status_code=503, detail=reason)
        with google_usage_context(operation="analysis", source_ids=[payload.left_id, payload.right_id]):
            analysis = analyze_remix_tweaks(db, runtime.settings, payload.left_id, payload.right_id)
        _touch_session_cookie(request, response, session_id)
        return analysis.payload()

    @app.get("/api/remix/uploads")
    def list_drafts(request: Request, db: Session = Depends(db_session),
                    limit: int = Query(default=20, ge=1, le=20), cursor: str | None = Query(default=None, max_length=256)):
        owner = require_owner(settings, db)
        statement = select(MediaItem).join(TelegramMessage).where(
            TelegramMessage.chat_id == _upload_chat_id(owner),
            MediaItem.origin == DASHBOARD_UPLOAD_ORIGIN,
            MediaItem.public_status != "published", MediaItem.processing_status == "processed")
        rows, next_cursor = _keyset_page(db, statement, MediaItem, limit, cursor)
        return {"drafts": [_item_payload(db, settings, row, session_id=owner) for row in rows
                           if source_eligible(db, settings, row, owner)], "next_cursor": next_cursor}

    @app.get("/api/remix/uploads/{item_id}")
    def get_draft(item_id: int, db: Session = Depends(db_session)):
        owner = require_owner(settings, db)
        item = _remix_media_item_or_404(db, settings, item_id, session_id=owner)
        message = db.get(TelegramMessage, item.telegram_message_id)
        if item.origin != DASHBOARD_UPLOAD_ORIGIN or message.chat_id != _upload_chat_id(owner):
            raise HTTPException(status_code=404, detail="Draft not found")
        return _item_payload(db, settings, item, session_id=owner)

    @app.get("/api/remix/jobs")
    def list_remix_jobs(request: Request, db: Session = Depends(db_session),
                        limit: int = Query(default=8, ge=1, le=20), cursor: str | None = Query(default=None, max_length=256)):
        owner = require_owner(settings, db)
        statement = select(RemixJobRecord).where(RemixJobRecord.session_id == owner).options(
            selectinload(RemixJobRecord.attempts))
        rows, next_cursor = _keyset_page(db, statement, RemixJobRecord, limit, cursor)
        return {"jobs": [_job_payload(job) for job in rows], "next_cursor": next_cursor}

    @app.get("/api/remix/jobs/{job_id}")
    def get_remix_job(
        job_id: str,
        request: Request,
        response: Response,
        session_id: str = Depends(require_private_owner),
        db: Session = Depends(db_session),
        include_sources: bool = False,
    ) -> dict[str, Any]:
        job = db.get(RemixJobRecord, job_id)
        if not job or job.session_id != session_id:
            raise HTTPException(status_code=404, detail="Job not found")
        _touch_session_cookie(request, response, session_id)
        payload = _job_payload(job)
        runtime: RuntimeState = request.app.state.runtime
        if include_sources:
            payload["source_items"] = []
            for source_id in (job.left_id, job.right_id):
                try:
                    source = _remix_media_item_or_404(db, runtime.settings, source_id, session_id=session_id)
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
                    continue
                payload["source_items"].append(_item_payload(db, runtime.settings, source, session_id=session_id))
        if job.status == "saved" and job.saved_media_item_id:
            media_item = db.get(MediaItem, job.saved_media_item_id)
            if media_item:
                payload["saved_item"] = _item_payload(db, runtime.settings, media_item, session_id=session_id,
                                                      private_identity=True)
        return payload

    @app.get("/api/remix/jobs/{job_id}/image")
    def get_remix_job_image(
        job_id: str,
        request: Request,
        session_id: str = Depends(require_private_owner),
        db: Session = Depends(db_session),
    ) -> FileResponse:
        runtime: RuntimeState = request.app.state.runtime
        job = db.get(RemixJobRecord, job_id)
        if not job or job.session_id != session_id:
            raise HTTPException(status_code=404, detail="Job not found")
        saved_item = db.get(MediaItem, job.saved_media_item_id) if job.saved_media_item_id else None
        path = _remix_job_image_path(runtime.settings, job, saved_item)
        if path is None:
            raise HTTPException(status_code=404, detail="Image not ready")
        return FileResponse(path)

    @app.get("/api/remix/jobs/{job_id}/card")
    def get_remix_job_card(
        job_id: str,
        request: Request,
        page: bool = False,
        source: Literal["A", "B"] | None = None,
        playback: bool = False,
        session_id: str = Depends(require_private_owner),
        db: Session = Depends(db_session),
    ) -> Response:
        runtime: RuntimeState = request.app.state.runtime
        job = db.get(RemixJobRecord, job_id)
        if not job or job.session_id != session_id:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status not in {"ready", "saving", "saved"}:
            raise HTTPException(status_code=409, detail="Remix card is not ready")
        saved_item = db.get(MediaItem, job.saved_media_item_id) if job.saved_media_item_id else None
        path = _remix_job_image_path(runtime.settings, job, saved_item)
        if path is None:
            raise HTTPException(status_code=404, detail="Image not ready")
        source_ids = [id if source_eligible(db, runtime.settings, db.get(MediaItem, id), session_id) else 0 for id in (job.left_id, job.right_id)]
        sources = _remix_card_sources(db, runtime.settings, source_ids, [True, True])
        if source:
            if playback:
                return _remix_source_playback_response(db, runtime.settings, source_ids, source)
            return _remix_source_image_response(sources, source)
        if page:
            base = f"/api/remix/jobs/{job.id}"
            return render_remix_page(
                image_url=f"{base}/image",
                sources=_remix_page_sources(sources, f"{base}/card"),
                source_videos=_remix_page_videos(db, runtime.settings, source_ids, f"{base}/card"),
                summary=str(((job.result_json or {}).get("result") or {}).get("summary") or ""),
                tweaks=job.tweaks_json or None,
            )
        image = render_remix_card(path, sources, job.tweaks_json or None)
        return Response(content=image, media_type="image/png", headers={
            **_public_share_media_headers(),
            "Content-Disposition": 'inline; filename="remix-card.png"',
        })

    @app.post("/api/remix/jobs/{job_id}/save")
    def save_remix_job(
        job_id: str,
        request: Request,
        response: Response,
        session_id: str = Depends(require_private_owner),
        db: Session = Depends(db_session),
    ) -> dict[str, Any]:
        job = db.scalar(
            select(RemixJobRecord)
            .where(RemixJobRecord.id == job_id)
            .with_for_update()
        )
        if not job or job.session_id != session_id:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status == "saved" and job.saved_media_item_id:
            runtime: RuntimeState = request.app.state.runtime
            media_item = _public_media_item_or_404(db, runtime.settings, job.saved_media_item_id)
            return {"status": "saved", "item": _item_payload(db, runtime.settings, media_item,
                                                            session_id=session_id, private_identity=True)}
        if job.status != "ready":
            raise HTTPException(status_code=400, detail="Only ready remix jobs can be saved")
        runtime: RuntimeState = request.app.state.runtime
        for source_id in (job.left_id, job.right_id):
            _remix_media_item_or_404(db, runtime.settings, source_id, session_id=session_id)
        result = _ready_remix_result_or_409(runtime.settings, job)
        job.status = "saving"
        job.message = "Saving remix to library"
        job.updated_at = _utc_now()
        with google_usage_context(operation="remix_save", job_id=job.id):
            saved = save_remix_result(
                db,
                runtime.settings,
                result,
                embed=runtime.settings.online_embeddings_enabled,
                export_static_catalog=False,
            )
        saved_item_id = int(saved["item"]["id"])
        media_item = db.get(MediaItem, saved_item_id)
        if media_item is None:
            raise HTTPException(status_code=500, detail="Saved remix item is missing")
        media_item.public_status = "published"
        media_item.visibility = "public"
        media_item.origin = "remix"
        media_item.public_original_allowed = True
        media_item.published_at = _utc_now()
        db.add_all(
            [
                RemixSourceRecord(
                    job_id=job.id,
                    remix_media_item_id=saved_item_id,
                    source_media_item_id=job.left_id,
                    position=0,
                ),
                RemixSourceRecord(
                    job_id=job.id,
                    remix_media_item_id=saved_item_id,
                    source_media_item_id=job.right_id,
                    position=1,
                ),
            ]
        )
        refresh_public_indexes(db, [job.left_id, job.right_id, saved_item_id], settings=runtime.settings)
        job.saved_media_item_id = saved_item_id
        job.embedding_error = saved.get("embedding_error")
        job.status = "saved"
        job.message = "Saved to library"
        job.updated_at = _utc_now()
        db.flush()
        _touch_session_cookie(request, response, session_id)
        return {
            "status": "saved",
            "item": _item_payload(db, runtime.settings, media_item, session_id=session_id, private_identity=True),
            "embedding_error": job.embedding_error,
        }

    @app.delete("/api/remix/items/{media_item_id}", status_code=204)
    def delete_saved_remix(
        media_item_id: int,
        request: Request,
        session_id: str = Depends(require_private_owner),
        db: Session = Depends(db_session),
    ) -> Response:
        """Remove a saved remix from the shared, proxy-authenticated Arsenal."""
        runtime: RuntimeState = request.app.state.runtime
        _require_same_origin(request, runtime.settings)
        _check_write_limit(request, session_id, "remix_delete", runtime.settings.public_write_actions_per_minute, 60)
        media_item = db.scalar(
            select(MediaItem).where(
                MediaItem.id == media_item_id,
                MediaItem.origin == "remix",
                MediaItem.public_status == "published",
                MediaItem.visibility == "public",
            ).with_for_update()
        )
        if media_item is None:
            raise HTTPException(status_code=404, detail="Saved remix not found")
        jobs = db.scalars(
            select(RemixJobRecord)
            .where(RemixJobRecord.saved_media_item_id == media_item_id)
            .with_for_update()
        ).all()
        used_as_source = db.scalar(select(RemixJobRecord.id).where(or_(
            RemixJobRecord.left_id == media_item_id, RemixJobRecord.right_id == media_item_id,
        )).limit(1))

        stored_paths = [
            media_item.original_path,
            media_item.thumbnail_path,
            media_item.preview_path,
            media_item.hover_preview_path,
            *db.scalars(select(MediaKeyframe.path).where(MediaKeyframe.media_item_id == media_item.id)).all(),
        ]
        message = db.get(TelegramMessage, media_item.telegram_message_id)
        for job in jobs:
            job.saved_media_item_id = None
            job.status = "deleted"
            job.message = "Saved remix deleted"
            job.result_json = {}
            job.updated_at = _utc_now()
        if used_as_source:
            # Preserve foreign keys without keeping deleted media accessible.
            media_item.public_status = "deleted"
            media_item.visibility = "private"
            media_item.processing_status = "deleted"
            media_item.derivative_hashes_json = None
            media_item.original_path = None
            media_item.thumbnail_path = None
            media_item.preview_path = None
            media_item.hover_preview_path = None
            media_item.public_share = None
            media_item.search_document = None
            media_item.public_tags.clear()
            media_item.keyframes.clear()
            media_item.classifications.clear()
            media_item.embeddings.clear()
            media_item.updated_at = _utc_now()
            db.execute(delete(RemixSourceRecord).where(RemixSourceRecord.remix_media_item_id == media_item_id))
            if message is not None:
                message.caption = "Deleted remix"
                message.raw_metadata_json = {}
        else:
            db.delete(media_item)
            if message is not None:
                db.delete(message)
        db.flush()
        for stored_path in stored_paths:
            path = reserve_absolute(runtime.settings, stored_path)
            if path is not None:
                path.unlink(missing_ok=True)
        return Response(status_code=204)

    @app.post("/api/remix/uploads")
    def upload_media_for_remix(
        request: Request,
        response: Response,
        file: UploadFile = File(...),
        session_id: str = Depends(require_private_owner),
        db: Session = Depends(db_session),
    ) -> dict[str, Any]:
        runtime: RuntimeState = request.app.state.runtime
        _check_write_limit(request, session_id, "upload", runtime.settings.public_write_actions_per_minute, 60)

        name = (file.filename or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Uploaded file is missing a name")
        suffix = Path(name).suffix.lower()
        if suffix not in REMIX_UPLOAD_EXTENSIONS:
            raise HTTPException(status_code=415, detail="Upload an image or video in a supported format")
        upload_dir = runtime.settings.remix_drafts_dir
        upload_dir.mkdir(parents=True, exist_ok=True)
        upload_path = upload_dir / f"{uuid.uuid4().hex}{suffix}"
        message_id: int | None = None
        media_item_id: int | None = None

        try:
            size_bytes = _write_limited_upload(
                file.file,
                upload_path,
                max_bytes=int(runtime.settings.public_upload_max_mb * 1024 * 1024),
            )

            upload_chat_id = _upload_chat_id(session_id)
            _lock_anonymous_session(db, session_id)
            next_message_id = db.scalar(
                select(func.max(TelegramMessage.telegram_message_id)).where(TelegramMessage.chat_id == upload_chat_id)
            )
            message = TelegramMessage(
                telegram_message_id=int(next_message_id or 0) + 1,
                chat_id=upload_chat_id,
                date=_utc_now(),
                caption=Path(name).stem,
                raw_metadata_json={
                    "upload_file_name": name,
                    "upload_size_bytes": size_bytes,
                },
                import_status="upload",
            )
            db.add(message)
            db.flush()
            message_id = message.id
            media_item = import_local_media_file(db, runtime.settings, message.id, upload_path, file.content_type)
            media_item_id = media_item.id
            media_item.origin = DASHBOARD_UPLOAD_ORIGIN
            media_item.public_status = "draft"
            media_item.visibility = "private"
            media_item.public_original_allowed = False
            db.flush()

            if not process_media_item(db, runtime.settings, media_item):
                detail = media_item.processing_error or "Unable to process uploaded media"
                raise HTTPException(status_code=400, detail=detail)
            try:
                with google_usage_context(operation="upload_classification", media_item_id=media_item.id):
                    classify_media_item(db, runtime.settings, media_item, force=True)
            except Exception as error:
                raise HTTPException(status_code=400, detail=f"Failed to classify uploaded media: {error}") from error

            refresh_public_indexes(db, [media_item.id], settings=runtime.settings)
            _touch_session_cookie(request, response, session_id)
            return _item_payload(db, runtime.settings, media_item, session_id=session_id)
        except Exception:
            if message_id is not None and media_item_id is not None:
                _cleanup_failed_upload(runtime.settings, message_id, media_item_id)
            raise
        finally:
            file.file.close()
            try:
                upload_path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass

    @app.post("/api/remix/uploads/{media_item_id}/save")
    def save_uploaded_media(
        media_item_id: int,
        request: Request,
        response: Response,
        session_id: str = Depends(require_private_owner),
        db: Session = Depends(db_session),
    ) -> dict[str, Any]:
        runtime: RuntimeState = request.app.state.runtime
        _check_write_limit(request, session_id, "upload", runtime.settings.public_write_actions_per_minute, 60)
        media_item = db.get(MediaItem, media_item_id)
        if media_item is None:
            raise HTTPException(status_code=404, detail="Item not found")
        if media_item.processing_status != "processed":
            raise HTTPException(status_code=400, detail="Uploaded media is not ready yet")
        message = db.get(TelegramMessage, media_item.telegram_message_id)
        if message is None or message.chat_id != _upload_chat_id(session_id) or media_item.origin != DASHBOARD_UPLOAD_ORIGIN:
            raise HTTPException(status_code=404, detail="Item not found")

        if media_item.public_status != "published" or media_item.visibility != "public":
            media_item.public_status = "published"
            media_item.visibility = "public"
            media_item.public_original_allowed = True
            media_item.published_at = media_item.published_at or _utc_now()
            media_item.updated_at = _utc_now()
            db.flush()
            refresh_public_indexes(db, [media_item.id], settings=runtime.settings)
        _touch_session_cookie(request, response, session_id)
        return {"status": "saved", "item": _item_payload(db, runtime.settings, media_item, session_id=session_id)}

    @app.get("/media/{path:path}")
    def public_media(path: str, request: Request) -> FileResponse:
        return _public_media_response(path, request)

    @app.head("/media/{path:path}")
    def public_media_head(path: str, request: Request) -> FileResponse:
        return _public_media_response(path, request)

    def _public_media_response(path: str, request: Request) -> FileResponse:
        runtime: RuntimeState = request.app.state.runtime
        relative = f"media/{path}".replace("\\", "/")
        if not _is_public_media_path(relative):
            raise HTTPException(status_code=404, detail="Media not found")
        session_id = require_owner(runtime.settings)
        with runtime.session_local() as db:
            if not _media_path_allowed(db, runtime.settings, relative, session_id=session_id):
                raise HTTPException(status_code=404, detail="Media not found")
        absolute = (runtime.settings.reserve_path / relative).resolve()
        reserve = runtime.settings.reserve_path.resolve()
        if not str(absolute).startswith(str(reserve)) or not absolute.exists() or not absolute.is_file():
            raise HTTPException(status_code=404, detail="Media not found")
        return FileResponse(absolute)

    @app.get("/share/{token}", response_class=HTMLResponse, dependencies=[Depends(limit_public_share)])
    @app.head("/share/{token}", dependencies=[Depends(limit_public_share)])
    @app.get("/{token:sharecode}", response_class=HTMLResponse, dependencies=[Depends(limit_public_share)])
    @app.head("/{token:sharecode}", dependencies=[Depends(limit_public_share)])
    def public_share_page(
        token: str,
        request: Request,
        db: Session = Depends(db_session),
    ) -> HTMLResponse:
        runtime: RuntimeState = request.app.state.runtime
        media_item = _active_public_share_or_404(db, runtime, token)
        alias = db.get(PublicShareCode, token) if re.fullmatch(SHARE_CODE_PATTERN, token) else None
        return _public_share_page_response(db, runtime, media_item, token, card=bool(alias and alias.card))

    @app.get("/share/{token}/card", response_class=HTMLResponse, dependencies=[Depends(limit_public_share)])
    @app.get("/{token:sharecode}/card", response_class=HTMLResponse, dependencies=[Depends(limit_public_share)])
    def public_remix_card_page(token: str, request: Request, db: Session = Depends(db_session)) -> HTMLResponse:
        runtime: RuntimeState = request.app.state.runtime
        item = _active_public_share_or_404(db, runtime, token, require_card=True)
        if item.origin != "remix":
            raise HTTPException(status_code=404, detail="Remix card not found")
        return _public_share_page_response(db, runtime, item, token, card=True)

    @app.get("/share/{token}/card/image", dependencies=[Depends(limit_public_share)])
    @app.get("/{token:sharecode}/card/image", dependencies=[Depends(limit_public_share)])
    def public_remix_card_image(
        token: str,
        request: Request,
        source: Literal["A", "B"] | None = None,
        playback: bool = False,
        db: Session = Depends(db_session),
    ) -> Response:
        runtime: RuntimeState = request.app.state.runtime
        item = _active_public_share_or_404(db, runtime, token, require_card=True)
        return _remix_card_response(db, runtime.settings, item, source=source, playback=playback)

    @app.get("/share/{token}/media", dependencies=[Depends(limit_public_share)])
    @app.head("/share/{token}/media", dependencies=[Depends(limit_public_share)])
    @app.get("/{token:sharecode}/media", dependencies=[Depends(limit_public_share)])
    @app.head("/{token:sharecode}/media", dependencies=[Depends(limit_public_share)])
    def public_share_media(token: str, request: Request) -> FileResponse:
        runtime: RuntimeState = request.app.state.runtime
        with runtime.session_local() as db:
            media_item = _active_public_share_or_404(db, runtime, token)
            media_item_id = media_item.id
            media_type = media_item.media_type
            source = _shared_media_source(runtime.settings, media_item)
        if media_type == "video":
            try:
                playback = ensure_browser_playback(runtime.settings, media_item_id, source)
                return FileResponse(playback, media_type="video/mp4", headers=_public_share_media_headers())
            except RuntimeError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Browser-compatible playback is still being prepared",
                    headers={"Retry-After": "10"},
                ) from exc
        return FileResponse(source, headers=_public_share_media_headers())

    @app.post("/share/{token}/events", status_code=204, dependencies=[Depends(limit_public_share)])
    @app.post("/{token:sharecode}/events", status_code=204, dependencies=[Depends(limit_public_share)])
    def public_share_event(
        token: str,
        payload: PlaybackDiagnostic,
        request: Request,
        db: Session = Depends(db_session),
    ) -> Response:
        runtime: RuntimeState = request.app.state.runtime
        _require_same_origin(request, runtime.settings)
        media_item = _active_public_share_or_404(db, runtime, token)
        _check_write_limit(request, f"share:{media_item.id}", "playback", 120, 60)
        log_playback_diagnostic(media_item.id, request.headers.get("user-agent", ""), payload)
        return Response(status_code=204)

    @app.get("/share/{token}/poster", dependencies=[Depends(limit_public_share)])
    @app.head("/share/{token}/poster", dependencies=[Depends(limit_public_share)])
    @app.get("/{token:sharecode}/poster", dependencies=[Depends(limit_public_share)])
    @app.head("/{token:sharecode}/poster", dependencies=[Depends(limit_public_share)])
    def public_share_poster(token: str, request: Request) -> FileResponse:
        runtime: RuntimeState = request.app.state.runtime
        with runtime.session_local() as db:
            media_item = _active_public_share_or_404(db, runtime, token)
            poster = _shared_poster_source(runtime.settings, media_item)
        return FileResponse(poster, headers=_public_share_media_headers())

    @app.get("/")
    def dashboard_root(request: Request) -> FileResponse:
        runtime: RuntimeState = request.app.state.runtime
        return _dashboard_file_or_404(runtime.settings, "index.html")

    @app.get("/{path:path}")
    def dashboard_asset_or_route(path: str, request: Request) -> FileResponse:
        if path.startswith(("api/", "media/", "share/", "catalog/", "data/")):
            raise HTTPException(status_code=404, detail="Not found")
        runtime: RuntimeState = request.app.state.runtime
        return _dashboard_file_or_404(runtime.settings, path)

    return app


def db_session(request: Request) -> Generator[Session, None, None]:
    runtime: RuntimeState = request.app.state.runtime
    session = runtime.session_local()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def require_private_owner(request: Request, response: Response) -> str:
    runtime: RuntimeState = request.app.state.runtime
    settings = runtime.settings
    session_id = require_owner(settings)
    # A heartbeat must not hold a row lock for the lifetime of an AI request.
    with runtime.session_local.begin() as db:
        session = db.get(AnonymousSession, session_id)
        now = _utc_now()
        if session is None:
            raise HTTPException(status_code=503, detail="Configured owner row missing")
        else:
            session = db.scalar(
                select(AnonymousSession)
                .where(
                    AnonymousSession.id == session_id,
                    AnonymousSession.last_seen_at < now - timedelta(minutes=1),
                )
                .with_for_update(skip_locked=True)
            )
            if session is not None:
                session.last_seen_at = now
    _touch_session_cookie(request, response, session_id)
    return session_id


def _session_id_from_cookie(request: Request) -> str | None:
    runtime: RuntimeState = request.app.state.runtime
    raw_cookie = request.cookies.get(runtime.settings.public_session_cookie_name)
    if not raw_cookie:
        return None
    try:
        loaded = runtime.signer.loads(raw_cookie)
    except BadSignature:
        return None
    if isinstance(loaded, dict) and isinstance(loaded.get("sid"), str):
        return str(loaded["sid"])
    return None


def _touch_session_cookie(request: Request, response: Response, session_id: str) -> None:
    runtime: RuntimeState = request.app.state.runtime
    settings = runtime.settings
    signed = runtime.signer.dumps({"sid": session_id})
    response.set_cookie(
        settings.public_session_cookie_name,
        signed,
        max_age=settings.public_session_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.public_session_cookie_secure,
        samesite="lax",
    )


def _check_write_limit(
    request: Request,
    session_id: str,
    action: str,
    max_events: int,
    window_seconds: int,
) -> None:
    runtime: RuntimeState = request.app.state.runtime
    limits = (
        (f"rate:{action}:session:{session_id}", max_events),
        (f"rate:{action}:ip:{_client_ip_hash(request)}", max_events * 2),
    )
    try:
        import redis

        client = redis.Redis.from_url(
            runtime.settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        for key, limit in limits:
            count = client.incr(key)
            if count == 1:
                client.expire(key, window_seconds)
            if count > limit:
                retry_after = client.ttl(key)
                if retry_after < 1:
                    retry_after = window_seconds
                raise _rate_limit_exception(action, limit, window_seconds, retry_after)
    except HTTPException:
        raise
    except Exception:
        # Local/dev fallback when Redis is not running.
        bucket = getattr(request.app.state, "_memory_rate_limits", {})
        now = datetime.now(timezone.utc).timestamp()
        for key, limit in limits:
            entries = [stamp for stamp in bucket.get(key, []) if now - stamp < window_seconds]
            if len(entries) >= limit:
                retry_after = window_seconds - int(now - min(entries))
                raise _rate_limit_exception(action, limit, window_seconds, retry_after)
            bucket[key] = [*entries, now]
        request.app.state._memory_rate_limits = bucket


def _rate_limit_exception(action: str, max_events: int, window_seconds: int, retry_after: int) -> HTTPException:
    retry_after = max(1, int(retry_after))
    window_minutes = max(1, window_seconds // 60)
    wait_minutes = max(1, (retry_after + 59) // 60)
    action_label = {
        "analysis": "remix analyses",
        "curation": "curation edits",
        "remix": "remix jobs",
        "upload": "uploads",
    }.get(action, "requests")
    return HTTPException(
        status_code=429,
        detail={
            "error": "rate_limit_exceeded",
            "message": (
                f"You've hit the {action_label} limit: {max_events} per rolling "
                f"{window_minutes} minutes. Try again in about {wait_minutes} minutes."
            ),
            "limit": max_events,
            "window_seconds": window_seconds,
            "retry_after_seconds": retry_after,
        },
        headers={"Retry-After": str(retry_after)},
    )


def _lock_anonymous_session(db: Session, session_id: str) -> None:
    session = db.scalar(
        select(AnonymousSession)
        .where(AnonymousSession.id == session_id)
        .with_for_update()
    )
    if session is None:
        raise HTTPException(status_code=409, detail="Session is not ready; retry the request")


def _write_limited_upload(source, destination: Path, *, max_bytes: int) -> int:
    total = 0
    with destination.open("wb") as target:
        while chunk := source.read(UPLOAD_CHUNK_BYTES):
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"Upload exceeds the {max_bytes // (1024 * 1024)} MB limit",
                )
            target.write(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    return total


def _cleanup_failed_upload(settings: Settings, message_id: int, media_item_id: int) -> None:
    shutil.rmtree(settings.originals_dir / str(message_id), ignore_errors=True)
    stem = f"media-{media_item_id:08d}"
    for directory in (
        settings.thumbnails_dir,
        settings.previews_dir,
        settings.video_previews_dir,
        settings.video_playback_dir,
        settings.keyframes_dir,
    ):
        for path in directory.glob(f"{stem}*"):
            if path.is_file():
                path.unlink(missing_ok=True)


def enqueue_remix_job(settings: Settings, job_id: str) -> None:
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        client.lpush("meme_arsenal:remix_jobs", job_id)
    except Exception:
        # The DB row is the source of truth; workers can also scan queued jobs.
        return


def _query_public_items(
    db: Session,
    settings: Settings,
    *,
    session_id: str,
    limit: int,
    cursor: str | None,
    q: str,
    media_type: str,
    topic: str,
    tone: str,
    reaction_type: str,
    meme_format: str,
    language: str,
    favorite: bool,
    archived: bool,
    include_hidden: bool = False,
) -> tuple[list[MediaItem], str | None, int]:
    filtered_statement = _public_media_filter_statement(
        dialect_name=_session_dialect_name(db),
        session_id=session_id,
        q=q,
        media_type=media_type,
        topic=topic,
        tone=tone,
        reaction_type=reaction_type,
        meme_format=meme_format,
        language=language,
        favorite=favorite,
        archived=archived,
        include_hidden=include_hidden,
    )
    total = int(
        db.scalar(
            select(func.count()).select_from(filtered_statement.with_only_columns(MediaItem.id).order_by(None).subquery())
        )
        or 0
    )
    catalog_message = aliased(TelegramMessage)
    # Match catalog_saved_at: Telegram save date, or upload/remix publication time.
    saved_at = func.coalesce(
        case((MediaItem.origin.in_(("dashboard_upload", "remix")), MediaItem.published_at)),
        catalog_message.date,
        MediaItem.created_at,
    )
    statement = filtered_statement.join(
        catalog_message, MediaItem.telegram_message_id == catalog_message.id,
    ).order_by(saved_at.desc(), MediaItem.id.desc())
    rows: list[MediaItem] = []
    scan_position: tuple[datetime, int] | None = None
    if cursor:
        try:
            if str(cursor).startswith("saved:"):
                timestamp, item_id = str(cursor).removeprefix("saved:").rsplit("|", 1)
                scan_position = (datetime.fromisoformat(timestamp), int(item_id))
            else:
                # Translate old ID/import-time cursors from already-open dashboards.
                anchor = db.get(MediaItem, int(str(cursor).rsplit("|", 1)[-1]))
                if anchor is None:
                    raise ValueError("Missing cursor item")
                scan_position = (catalog_saved_at(anchor, anchor.telegram_message), anchor.id)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid catalog cursor; refresh the gallery") from exc
    next_cursor = None
    while len(rows) < limit:
        page_size = max(1, limit - len(rows))
        page_statement = statement
        if scan_position is not None:
            saved_before, item_id = scan_position
            page_statement = page_statement.where(or_(
                saved_at < saved_before,
                and_(saved_at == saved_before, MediaItem.id < item_id),
            ))
        raw_rows = (
            db.scalars(
                page_statement.options(
                    joinedload(MediaItem.telegram_message),
                    joinedload(MediaItem.curation),
                    selectinload(MediaItem.classifications),
                ).limit(page_size)
            )
            .unique()
            .all()
        )
        if not raw_rows:
            return rows, None, total
        rows.extend(row for row in raw_rows if _media_item_is_available(settings, row))
        last = raw_rows[-1]
        scan_position = (catalog_saved_at(last, last.telegram_message), last.id)
        next_cursor = f"saved:{scan_position[0].isoformat()}|{last.id}"
        if len(raw_rows) < page_size:
            return rows[:limit], None, total
    return rows[:limit], next_cursor, total


def _query_random_public_item(
    db: Session,
    settings: Settings,
    *,
    session_id: str,
    q: str,
    media_type: str,
    topic: str,
    tone: str,
    reaction_type: str,
    meme_format: str,
    language: str,
    favorite: bool,
    archived: bool,
) -> MediaItem | None:
    statement = _public_media_filter_statement(
        dialect_name=_session_dialect_name(db),
        session_id=session_id,
        q=q,
        media_type=media_type,
        topic=topic,
        tone=tone,
        reaction_type=reaction_type,
        meme_format=meme_format,
        language=language,
        favorite=favorite,
        archived=False,
    )
    rows = [
        row
        for row in db.scalars(statement).unique().all()
        if _media_item_is_available(settings, row)
    ]
    return random.choice(rows) if rows else None


def _public_media_filter_statement(
    *,
    dialect_name: str = "",
    session_id: str,
    q: str,
    media_type: str,
    topic: str,
    tone: str,
    reaction_type: str,
    meme_format: str,
    language: str,
    favorite: bool,
    archived: bool,
    include_hidden: bool = False,
):
    statement = _public_media_statement()
    if media_type != "all":
        statement = statement.where(MediaItem.media_type == media_type)
    tag_filters = {
        "topics": topic,
        "tone": tone,
        "reaction_type": reaction_type,
        "meme_format": meme_format,
        "language": language,
    }
    for tag_type, value in tag_filters.items():
        if value != "all":
            statement = statement.where(
                select(MediaTag.id)
                .where(MediaTag.media_item_id == MediaItem.id, MediaTag.tag_type == tag_type, MediaTag.value == value)
                .exists()
            )
    if favorite or archived:
        statement = statement.join(
            AnonymousSessionCuration,
            and_(
                AnonymousSessionCuration.media_item_id == MediaItem.id,
                AnonymousSessionCuration.session_id == session_id,
            ),
        )
        if favorite:
            statement = statement.where(AnonymousSessionCuration.favorite.is_(True))
        if archived:
            statement = statement.where(AnonymousSessionCuration.archived.is_(True))
    if not archived and not include_hidden:
        statement = statement.where(
            ~select(AnonymousSessionCuration.id)
            .where(
                AnonymousSessionCuration.media_item_id == MediaItem.id,
                AnonymousSessionCuration.session_id == session_id,
                AnonymousSessionCuration.archived.is_(True),
            )
            .correlate(MediaItem)
            .exists()
        )
    query = q.strip()
    if query:
        tsquery = _postgres_prefix_tsquery(query) if dialect_name == "postgresql" else None
        if tsquery:
            statement = statement.where(
                select(PublicSearchDocument.media_item_id)
                .where(
                    PublicSearchDocument.media_item_id == MediaItem.id,
                    func.to_tsvector(literal_column("'simple'"), PublicSearchDocument.document_text).op("@@")(
                        func.to_tsquery(literal_column("'simple'"), tsquery)
                    ),
                )
                .exists()
            )
        else:
            like = f"%{query.lower()}%"
            statement = statement.join(TelegramMessage)
            statement = statement.where(
                or_(
                    func.lower(TelegramMessage.caption).like(like),
                    select(Classification.id)
                    .where(
                        Classification.media_item_id == MediaItem.id,
                        or_(
                            func.lower(Classification.summary).like(like),
                            func.lower(Classification.visible_text).like(like),
                            func.lower(Classification.narration).like(like),
                        ),
                    )
                    .exists(),
                    select(PublicSearchDocument.media_item_id)
                    .where(
                        PublicSearchDocument.media_item_id == MediaItem.id,
                        func.lower(PublicSearchDocument.document_text).like(like),
                    )
                    .exists(),
                )
            )
    return statement


def _session_dialect_name(db: Session) -> str:
    bind = db.get_bind()
    return bind.dialect.name if bind is not None else ""


def _postgres_prefix_tsquery(query: str) -> str:
    tokens = re.findall(r"[a-zA-Z0-9]{2,}", query.lower())
    return " & ".join(f"{token}:*" for token in tokens)


def _public_media_statement():
    return select(MediaItem).where(
        MediaItem.public_status == "published",
        MediaItem.visibility == "public",
        MediaItem.processing_status == "processed",
    )


def _public_media_item_or_404(db: Session, settings: Settings, media_item_id: int) -> MediaItem:
    media_item = db.scalar(_public_media_statement().where(MediaItem.id == media_item_id))
    if not media_item or not _media_item_is_available(settings, media_item):
        raise HTTPException(status_code=404, detail="Item not found")
    return media_item


def _playback_media_item_or_404(
    db: Session,
    settings: Settings,
    media_item_id: int,
    *,
    session_id: str | None,
) -> MediaItem:
    media_item = db.get(MediaItem, media_item_id)
    if (
        media_item is None
        or media_item.media_type != "video"
        or media_item.processing_status != "processed"
        or not media_item_has_original_file(settings, media_item)
    ):
        raise HTTPException(status_code=404, detail="Video not found")
    if media_item.public_status == "published" and media_item.visibility == "public":
        if not media_item.public_original_allowed:
            raise HTTPException(status_code=404, detail="Video not found")
        return media_item
    if not session_id or media_item.origin != DASHBOARD_UPLOAD_ORIGIN:
        raise HTTPException(status_code=404, detail="Video not found")
    message = db.get(TelegramMessage, media_item.telegram_message_id)
    if message is None or message.chat_id != _upload_chat_id(session_id):
        raise HTTPException(status_code=404, detail="Video not found")
    return media_item


def _shareable_media_item_or_404(
    db: Session,
    settings: Settings,
    media_item_id: int,
) -> MediaItem:
    media_item = db.get(MediaItem, media_item_id)
    if media_item is None or not _should_index_public_item(settings, media_item):
        raise HTTPException(status_code=404, detail="Item not found")
    return media_item


def _require_same_origin(request: Request, settings: Settings) -> None:
    origin = request.headers.get("origin")
    allowed_origins = {
        settings.public_site_url.rstrip("/"),
        str(request.base_url).rstrip("/"),
    }
    if origin and origin.rstrip("/") not in allowed_origins:
        raise HTTPException(status_code=403, detail="Cross-origin write rejected")


def _locked_shareable_item(db: Session, settings: Settings, media_item_id: int, *, require_eligible: bool = True) -> MediaItem:
    item = db.scalar(select(MediaItem).where(MediaItem.id == media_item_id)
                     .with_for_update().execution_options(populate_existing=True))
    if item is None or (require_eligible and not _should_index_public_item(settings, item)):
        raise HTTPException(status_code=404, detail="Item not found")
    return item


def _capture_recipe_grant(db: Session, settings: Settings, item: MediaItem) -> dict:
    links = db.scalars(select(RemixSourceRecord).where(RemixSourceRecord.remix_media_item_id == item.id)).all()
    grant = {"A": None, "B": None}
    for side, position in (("A", 0), ("B", 1)):
        link = next((link for link in links if link.position == position), None)
        source = db.get(MediaItem, link.source_media_item_id) if link else None
        if source is not None and source.media_type in {"image", "video"} and _should_index_public_item(settings, source):
            grant[side] = {"id": source.id, "original": bool(source.public_original_allowed)}
    return grant


def _public_share_code(db: Session, share: PublicMediaShare, *, card: bool) -> str:
    for _ in range(20):
        existing = db.scalar(
            select(PublicShareCode).where(
                PublicShareCode.token_id == share.token_id,
                PublicShareCode.card.is_(card),
            )
        )
        if existing is not None:
            return existing.code
        code = "".join(secrets.choice(SHARE_CODE_ALPHABET) for _ in range(5))
        try:
            with db.begin_nested():
                db.add(PublicShareCode(code=code, token_id=share.token_id, card=card))
                db.flush()
            return code
        except IntegrityError:
            # Retry a collision, or reuse the alias created by a concurrent request.
            continue
    raise HTTPException(status_code=503, detail="Could not create a share link; please retry")


def _public_share_payload(
    db: Session,
    runtime: RuntimeState,
    share: PublicMediaShare | None,
    *,
    card: bool = False,
) -> dict[str, Any]:
    active = bool(share and share.active and (not card or share.recipe_grant_json is not None))
    url = None
    media_url = None
    card_url = None
    if active and share is not None and runtime.settings.public_sharing_enabled:
        if runtime.settings.public_share_url:
            alias = db.scalar(select(PublicShareCode).where(PublicShareCode.token_id == share.token_id, PublicShareCode.card.is_(card)))
            url = f"{runtime.settings.public_share_url.rstrip('/')}/{alias.code}" if alias else None
            card_url = url if card else None
        else:
            token = runtime.share_signer.dumps({"share": share.token_id, **({"card": True} if card else {})})
            url = f"{runtime.settings.public_site_url.rstrip('/')}/share/{token}"
            card_url = f"{url}/card" if card else None
        # Older dashboards share this field directly; keep public links suffix-free.
        media_url = url if runtime.settings.public_share_url else f"{url}/media"
    return {
        "active": active,
        "enabled": runtime.settings.public_sharing_enabled,
        "variant": "recipe" if card else "output",
        "url": url,
        "media_url": media_url,
        "card_url": card_url,
        "created_at": _share_timestamp(share.created_at) if active and share else None,
        "revoked_at": _share_timestamp(share.revoked_at) if share and share.revoked_at else None,
    }


def _share_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _active_public_share_or_404(
    db: Session,
    runtime: RuntimeState,
    token: str,
    *,
    require_card: bool = False,
) -> MediaItem:
    if not runtime.settings.public_sharing_enabled:
        raise HTTPException(status_code=404, detail="Shared item not found")
    if not token or len(token) > 512:
        raise HTTPException(status_code=404, detail="Shared item not found")
    if re.fullmatch(SHARE_CODE_PATTERN, token):
        alias = db.get(PublicShareCode, token)
        if alias is None:
            raise HTTPException(status_code=404, detail="Shared item not found")
        payload = {"share": alias.token_id, "card": alias.card}
    else:
        try:
            payload = runtime.share_signer.loads(token)
        except BadSignature as exc:
            raise HTTPException(status_code=404, detail="Shared item not found") from exc
    token_id = payload.get("share") if isinstance(payload, dict) else None
    if require_card and (not isinstance(payload, dict) or payload.get("card") is not True):
        raise HTTPException(status_code=404, detail="Remix card not found")
    if not isinstance(token_id, str) or not token_id:
        raise HTTPException(status_code=404, detail="Shared item not found")
    share = db.scalar(
        select(PublicMediaShare).where(
            PublicMediaShare.token_id == token_id,
            PublicMediaShare.active.is_(True),
        )
    )
    if share is None:
        raise HTTPException(status_code=404, detail="Shared item not found")
    if (require_card or payload.get("card") is True) and share.recipe_grant_json is None:
        raise HTTPException(status_code=404, detail="Remix card not found")
    media_item = db.get(MediaItem, share.media_item_id)
    if media_item is None or not _should_index_public_item(runtime.settings, media_item):
        raise HTTPException(status_code=404, detail="Shared item not found")
    return media_item


def _first_stored_media_file(settings: Settings, paths: list[str | None]) -> Path | None:
    for stored_path in paths:
        absolute = reserve_absolute(settings, stored_path)
        if absolute is not None and absolute.exists() and absolute.is_file():
            return absolute
    return None


def _shared_media_source(settings: Settings, media_item: MediaItem) -> Path:
    if media_item.media_type == "video" or media_item.mime_type == "image/gif":
        candidates = [media_item.original_path]
    else:
        candidates = [media_item.preview_path, media_item.original_path]
    source = _first_stored_media_file(settings, candidates)
    if source is None:
        raise HTTPException(status_code=404, detail="Shared media not found")
    return source


def _shared_poster_source_or_none(settings: Settings, media_item: MediaItem) -> Path | None:
    candidates = [media_item.preview_path, media_item.thumbnail_path]
    if media_item.media_type != "video":
        candidates.append(media_item.original_path)
    return _first_stored_media_file(settings, candidates)


def _shared_poster_source(settings: Settings, media_item: MediaItem) -> Path:
    poster = _shared_poster_source_or_none(settings, media_item)
    if poster is None:
        raise HTTPException(status_code=404, detail="Shared poster not found")
    return poster


def _public_share_media_headers() -> dict[str, str]:
    return {
        "Cache-Control": "private, no-store",
        "Cross-Origin-Resource-Policy": "same-origin",
        "X-Robots-Tag": "noindex, nofollow, noarchive",
    }


def _compact_share_text(value: str | None, limit: int) -> str:
    compact = re.sub(r"\s+", " ", value or "").strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)].rstrip() + "..."


def _remix_card_sources(db: Session, settings: Settings, source_ids: list[int], originals: list[bool]) -> list[tuple[str, Path | None]]:
    sources = []
    for index, source_id in enumerate(source_ids[:2]):
        source = db.get(MediaItem, source_id)
        classification = _latest_classification(db, source.id) if source else None
        sources.append((classification.summary if classification else "Source unavailable",
                        _first_stored_media_file(settings, [source.preview_path, source.thumbnail_path] +
                            ([source.original_path] if originals[index] and source.media_type != "video" else [])) if source else None))
    return sources


def _remix_source_image_response(sources: list[tuple[str, Path | None]], side: str) -> Response:
    index = 0 if side == "A" else 1
    path = sources[index][1] if index < len(sources) else None
    if path is None:
        raise HTTPException(status_code=404, detail="Source unavailable")
    return FileResponse(path, headers=_public_share_media_headers())


def _remix_page_sources(sources: list[tuple[str, Path | None]], image_base: str) -> list[tuple[str, str | None]]:
    return [(description, f"{image_base}?source={side}" if path else None)
            for side, (description, path) in zip(("A", "B"), sources)]


def _remix_page_videos(db: Session, settings: Settings, source_ids: list[int], image_base: str) -> dict[str, str]:
    videos = {}
    for side, source_id in zip(("A", "B"), source_ids):
        source = db.get(MediaItem, source_id)
        if source and source.media_type == "video" and media_item_has_original_file(settings, source):
            videos[side] = f"{image_base}?source={side}&playback=true"
    return videos


def _remix_source_playback_response(db: Session, settings: Settings, source_ids: list[int], side: str) -> Response:
    index = 0 if side == "A" else 1
    source = db.get(MediaItem, source_ids[index]) if index < len(source_ids) else None
    if source is None or source.media_type != "video":
        raise HTTPException(status_code=404, detail="Video source unavailable")
    original = _shared_media_source(settings, source)
    try:
        playback = ensure_browser_playback(settings, source.id, original)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503, detail="Browser-compatible playback is still being prepared",
            headers={"Retry-After": "10"},
        ) from exc
    return FileResponse(playback, media_type="video/mp4", headers=_public_share_media_headers())


def _remix_card_response(
    db: Session,
    settings: Settings,
    item: MediaItem,
    *,
    page: bool = False,
    source: str | None = None,
    playback: bool = False,
    image_base: str = "",
    output_url: str = "",
    public_url: str | None = None,
    private_owner: str | None = None,
) -> Response:
    if item.origin != "remix":
        raise HTTPException(status_code=404, detail="Remix card not found")
    links = db.scalars(select(RemixSourceRecord).where(
        RemixSourceRecord.remix_media_item_id == item.id,
    ).order_by(RemixSourceRecord.position, RemixSourceRecord.id)).all()
    source_ids = [next((link.source_media_item_id for link in links if link.position == position), 0)
                  for position in (0, 1)]
    if private_owner is not None:
        source_ids = [id if source_eligible(db, settings, db.get(MediaItem, id), private_owner) else 0 for id in source_ids]
        originals = [True, True]
    else:
        share = db.scalar(select(PublicMediaShare).where(PublicMediaShare.media_item_id == item.id))
        if share is None or not share.active or share.recipe_grant_json is None:
            raise HTTPException(status_code=404, detail="Remix card not found")
        originals = [False, False]
        for index, side in enumerate(("A", "B")):
            approved = share.recipe_grant_json.get(side)
            source_item = db.get(MediaItem, source_ids[index]) if approved and approved.get("id") == source_ids[index] else None
            if source_item is None or source_item.media_type not in {"image", "video"} or not _should_index_public_item(settings, source_item):
                source_ids[index] = 0
            else:
                originals[index] = bool(approved.get("original") and source_item.public_original_allowed)
    sources = _remix_card_sources(db, settings, source_ids, originals)
    video_ids = [id if originals[index] else 0 for index, id in enumerate(source_ids)]
    message = db.get(TelegramMessage, item.telegram_message_id)
    metadata = message.raw_metadata_json if message and isinstance(message.raw_metadata_json, dict) else {}
    tweaks = metadata.get("remix_tweaks")
    if source:
        if playback:
            return _remix_source_playback_response(db, settings, video_ids, source)
        return _remix_source_image_response(sources, source)
    if page:
        classification = _latest_classification(db, item.id)
        return render_remix_page(
            image_url=output_url or f"/{item.original_path}",
            sources=_remix_page_sources(sources, image_base),
            source_videos=_remix_page_videos(db, settings, video_ids, image_base),
            summary=classification.summary if classification else "",
            tweaks=tweaks if isinstance(tweaks, dict) else None,
            public_url=public_url,
        )
    image = render_remix_card(_shared_poster_source(settings, item), sources, tweaks if isinstance(tweaks, dict) else None)
    return Response(content=image, media_type="image/png", headers={
        **_public_share_media_headers(),
        "Content-Disposition": f'inline; filename="remix-{item.id}-card.png"',
    })


def _public_share_page_response(
    db: Session,
    runtime: RuntimeState,
    media_item: MediaItem,
    token: str,
    *,
    card: bool = False,
) -> HTMLResponse:
    if card:
        short_link = bool(re.fullmatch(SHARE_CODE_PATTERN, token))
        share_path = f"/{token}" if short_link else f"/share/{token}"
        origin = (runtime.settings.public_share_url if short_link else runtime.settings.public_site_url).rstrip("/")
        return _remix_card_response(
            db, runtime.settings, media_item, page=True,
            image_base=f"{share_path}/card/image", output_url=f"{origin}{share_path}/media",
            public_url=f"{origin}{share_path}{'' if short_link else '/card'}",
        )
    message = db.get(TelegramMessage, media_item.telegram_message_id)
    classification = _latest_classification(db, media_item.id)
    description = (classification.summary or "").strip() if classification else ""
    if not description and media_item.origin != "remix" and message:
        description = (message.caption or "").strip()
    description = description or "No description available."
    title = "Forward Deployed Meme"
    meta_description = _compact_share_text(description, 280)
    nonce = uuid.uuid4().hex
    escaped_title = html.escape(title)
    escaped_description = html.escape(description)
    escaped_meta_description = html.escape(meta_description, quote=True)
    escaped_token = html.escape(token, quote=True)
    # Lucide Info, Share2, and X icons, kept inline for the standalone public page.
    icon_attributes = 'width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"'
    info_icon = f'<svg {icon_attributes}><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>'
    share_icon = f'<svg {icon_attributes}><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.59 13.51 6.83 3.98M15.41 6.51l-6.82 3.98"/></svg>'
    close_icon = f'<svg {icon_attributes}><path d="M18 6 6 18M6 6l12 12"/></svg>'
    short_link = bool(re.fullmatch(SHARE_CODE_PATTERN, token))
    share_path = f"/{escaped_token}" if short_link else f"/share/{escaped_token}"
    share_origin = (
        runtime.settings.public_share_url if short_link else runtime.settings.public_site_url
    ).rstrip("/")
    page_url = html.escape(
        f"{share_origin}{share_path}",
        quote=True,
    )
    poster_available = _shared_poster_source_or_none(runtime.settings, media_item) is not None
    poster_path = f"{share_path}/poster" if poster_available else ""
    absolute_poster = (
        html.escape(
            f"{share_origin}{share_path}/poster",
            quote=True,
        )
        if poster_available
        else ""
    )
    media_path = f"{share_path}/media?v={BROWSER_PLAYBACK_VERSION}"
    if media_item.media_type == "video":
        poster_attribute = f' poster="{poster_path}"' if poster_path else ""
        media_markup = (
            f'<video src="{media_path}"{poster_attribute} controls playsinline preload="metadata" '
            'disablepictureinpicture disableremoteplayback controlslist="nodownload noremoteplayback" '
            'aria-label="Shared video"></video>'
        )
    else:
        media_markup = f'<img src="{media_path}" alt="{html.escape(_compact_share_text(description, 280), quote=True)}" loading="eager">'

    og_media = f'<meta property="og:image" content="{absolute_poster}">' if absolute_poster else ""
    if media_item.media_type == "video":
        absolute_media = html.escape(
            f"{share_origin}{share_path}/media",
            quote=True,
        )
        og_media += (
            f'<meta property="og:video" content="{absolute_media}">'
            '<meta property="og:video:type" content="video/mp4">'
        )

    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="description" content="{escaped_meta_description}">
  <meta name="robots" content="noindex, nofollow, noarchive">
  <meta property="og:type" content="website">
  <meta property="og:title" content="{html.escape(title, quote=True)}">
  <meta property="og:description" content="{escaped_meta_description}">
  <meta property="og:url" content="{page_url}">
  {og_media}
  <meta name="twitter:card" content="summary_large_image">
  <title>{escaped_title}</title>
  <style nonce="{nonce}">
    :root {{ color-scheme: light; --ink: #16120b; --pink: #ff7ad9; --yellow: #fbff12; --mint: #18f4d6; }}
    * {{ box-sizing: border-box; letter-spacing: 0; }}
    html {{ height: 100%; background: #fff; }}
    body {{ height: 100vh; height: 100dvh; margin: 0; display: grid; grid-template-rows: auto minmax(0, 1fr); overflow: hidden; color: var(--ink); font: 16px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
      background: linear-gradient(90deg, #16120b09 1px, transparent 1px) 0 0 / 18px 18px, linear-gradient(0deg, #16120b09 1px, transparent 1px) 0 0 / 18px 18px, linear-gradient(135deg, #faffbc, #ffd5f1 56%, #b9fff2); }}
    .toolbar {{ min-width: 0; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: calc(6px + env(safe-area-inset-top)) max(12px, env(safe-area-inset-right)) 6px max(12px, env(safe-area-inset-left)); border-bottom: 1px solid var(--ink); background: linear-gradient(100deg, var(--pink), #faff9c 50%, var(--mint)); }}
    h1 {{ min-width: 0; margin: 0; font-size: 18px; font-weight: 850; line-height: 1.15; overflow-wrap: anywhere; }}
    .actions {{ display: flex; flex: 0 0 auto; gap: 6px; }}
    .tool {{ display: inline-flex; align-items: center; justify-content: center; gap: 8px; flex: 0 0 auto; width: 44px; height: 44px; padding: 0; border: 1px solid var(--ink); border-radius: 0; background: var(--yellow); color: var(--ink); cursor: pointer; text-decoration: none; font: inherit; font-weight: 750; touch-action: manipulation; transition: background 150ms ease, transform 150ms ease; }}
    .tool svg {{ flex: 0 0 auto; }}
    .tool:hover {{ background: var(--mint); }}
    .tool:active {{ transform: translateY(1px); }}
    .tool:focus-visible {{ outline: 3px solid #1238ff; outline-offset: 2px; }}
    .tool:disabled {{ opacity: 0.65; cursor: wait; }}
    .shareButton {{ width: 104px; color: #fff; background: var(--ink); }}
    .shareButton:hover {{ background: #333; }}
    .shareCard {{ min-width: 0; min-height: 0; padding: 0 env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left); }}
    .media {{ width: 100%; height: 100%; }}
    .media img, .media video {{ display: block; width: 100%; height: 100%; object-fit: contain; }}
    .details {{ position: fixed; inset: calc(76px + env(safe-area-inset-top)) max(12px, env(safe-area-inset-right)) auto auto; margin: 0; padding: 0; width: min(440px, calc(100% - 24px)); max-height: calc(100vh - 100px); max-height: calc(100dvh - 100px - env(safe-area-inset-top)); border: 1px solid var(--ink); border-radius: 0; background: #fff5fc; color: var(--ink); box-shadow: 2px 2px 0 #16120b26; overflow: hidden; transform-origin: top right; }}
    .details[open] {{ display: flex; flex-direction: column; animation: reveal 200ms ease-out; }}
    .details::backdrop {{ background: #16120b38; }}
    .detailsHeader {{ flex-shrink: 0; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 12px; border-bottom: 1px solid var(--ink); background: var(--pink); }}
    h2 {{ margin: 0; font-size: 16px; line-height: 1.4; overflow-wrap: anywhere; }}
    .description {{ min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 20px; }}
    .description p {{ margin: 0; font-size: 16px; line-height: 1.65; white-space: pre-wrap; overflow-wrap: anywhere; }}
    .shareStatus {{ position: fixed; z-index: 2; inset: auto 12px calc(12px + env(safe-area-inset-bottom)); width: fit-content; max-width: calc(100% - 24px); margin: 0 auto; padding: 10px 16px; border: 1px solid var(--ink); background: var(--mint); color: var(--ink); overflow-wrap: anywhere; }}
    @keyframes reveal {{ from {{ opacity: 0; transform: perspective(700px) rotateY(-10deg) translateX(12px); }} to {{ opacity: 1; transform: none; }} }}
    @media (max-width: 600px) {{
      h1 {{ font-size: 16px; }}
      .toolbar {{ gap: 8px; }}
      .shareButton {{ width: 44px; }}
      .shareLabel {{ display: none; }}
      .details {{ inset: auto 8px calc(8px + env(safe-area-inset-bottom)); width: auto; max-height: 70vh; max-height: 70dvh; transform-origin: bottom right; }}
      .description {{ padding: 16px; }}
    }}
    @media (prefers-reduced-motion: reduce) {{ .details[open] {{ animation: none; }} .tool {{ transition: none; }} }}
  </style>
</head>
<body>
  <header class="toolbar">
    <h1>{escaped_title}</h1>
    <nav class="actions" aria-label="Shared meme actions">
      <button class="tool shareButton" id="share" type="button" aria-label="Share meme" title="Share meme">{share_icon}<span class="shareLabel">Share</span></button>
      <button class="tool" id="info" type="button" aria-label="Show description" title="Show description" aria-controls="details" aria-expanded="false" aria-haspopup="dialog">{info_icon}</button>
    </nav>
  </header>
  <main class="shareCard">
    <div class="media">{media_markup}</div>
  </main>
  <dialog class="details" id="details" aria-labelledby="descriptionTitle">
    <header class="detailsHeader"><h2 id="descriptionTitle">Description</h2><button class="tool" id="closeInfo" type="button" aria-label="Close description" title="Close description" autofocus>{close_icon}</button></header>
    <div class="description" tabindex="0"><p>{escaped_description}</p></div>
  </dialog>
  <p class="shareStatus" id="shareStatus" role="status" hidden></p>
  <script nonce="{nonce}">
    const info = document.getElementById("info");
    const details = document.getElementById("details");
    info.addEventListener("click", () => {{
      details.showModal();
      info.setAttribute("aria-expanded", "true");
    }});
    document.getElementById("closeInfo").addEventListener("click", () => details.close());
    details.addEventListener("close", () => {{
      info.setAttribute("aria-expanded", "false");
      info.focus({{ preventScroll: true }});
    }});
    details.addEventListener("click", (event) => {{
      const rect = details.getBoundingClientRect();
      if (event.target === details && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) details.close();
    }});
    const feedback = document.getElementById("shareStatus");
    let feedbackTimer;
    const notifyShare = (message) => {{
      clearTimeout(feedbackTimer);
      feedback.textContent = message;
      feedback.hidden = false;
      feedbackTimer = setTimeout(() => {{ feedback.hidden = true; }}, 2200);
    }};
    const video = document.querySelector("video");
    if (video) {{
      const attemptId = () => crypto.randomUUID?.() || Date.now() + "-" + Math.random().toString(36).slice(2);
      let attempt = attemptId();
      let started = performance.now();
      let sent = new Set();
      let stallTimer;
      const report = (event) => {{
        try {{
        if (sent.has(event)) return;
        sent.add(event);
        fetch("{share_path}/events", {{
          method: "POST", headers: {{ "Content-Type": "application/json" }}, keepalive: true,
          body: JSON.stringify({{
            attempt, event, surface: "share", source: "share",
            elapsed_ms: Math.min(86400000, Math.round(performance.now() - started)),
            current_time: Number.isFinite(video.currentTime) ? video.currentTime : 0,
            ready_state: video.readyState, network_state: video.networkState,
            buffered_end: video.buffered.length ? video.buffered.end(video.buffered.length - 1) : 0,
            paused: video.paused, muted: video.muted, error_code: video.error?.code || null,
            h264_aac: video.canPlayType('video/mp4; codecs="avc1.42E01E, mp4a.40.2"')
          }})
        }}).catch(() => {{}});
        }} catch {{ /* Playback and sharing do not depend on telemetry. */ }}
      }};
      video.addEventListener("play", () => {{
        if (sent.has("playing")) {{ attempt = attemptId(); started = performance.now(); sent = new Set(); }}
        report("play");
        clearTimeout(stallTimer);
        stallTimer = setTimeout(() => report("stalled"), 4000);
      }});
      video.addEventListener("playing", () => {{ clearTimeout(stallTimer); report("playing"); }});
      video.addEventListener("error", () => {{ clearTimeout(stallTimer); report("error"); }});
      video.addEventListener("waiting", () => {{
        clearTimeout(stallTimer);
        stallTimer = setTimeout(() => report("stalled"), 2500);
      }});
      video.addEventListener("pause", () => clearTimeout(stallTimer));
      video.addEventListener("ended", () => clearTimeout(stallTimer));
    }}
    const button = document.getElementById("share");
    const copyLink = async () => {{
      if (navigator.clipboard && window.isSecureContext) {{
        await navigator.clipboard.writeText(window.location.href);
        return;
      }}
      const field = document.createElement("textarea");
      field.value = window.location.href;
      field.setAttribute("readonly", "");
      field.style.position = "fixed";
      field.style.opacity = "0";
      document.body.appendChild(field);
      field.select();
      try {{
        if (!document.execCommand("copy")) throw new Error("Copy failed");
      }} finally {{ field.remove(); }}
    }};
    button.addEventListener("click", async () => {{
      button.disabled = true;
      try {{
        if (navigator.share) {{
          await navigator.share({{ title: document.title, url: window.location.href }});
        }} else {{
          await copyLink();
          notifyShare("Link copied");
        }}
      }} catch (error) {{
        if (!error || error.name !== "AbortError") {{
          try {{
            await copyLink();
            notifyShare("Link copied");
          }} catch {{
            notifyShare("Could not share. Please try again.");
          }}
        }}
      }} finally {{
        button.disabled = false;
      }}
    }});
  </script>
</body>
</html>"""
    csp = (
        "default-src 'none'; "
        "img-src 'self'; media-src 'self'; connect-src 'self'; "
        f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return HTMLResponse(
        page,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": csp,
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
        },
    )


def _media_item_is_available(settings: Settings, media_item: MediaItem) -> bool:
    return media_item.processing_status == "processed" and media_item_has_original_file(settings, media_item)


def _upload_chat_id(session_id: str) -> str:
    return f"{UPLOAD_CHAT_PREFIX}:{session_id}"


def _remix_media_item_or_404(
    db: Session,
    settings: Settings,
    media_item_id: int,
    *,
    session_id: str,
) -> MediaItem:
    media_item = db.get(MediaItem, media_item_id)
    if media_item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    # Authorize before revealing readiness or checking files for private uploads.
    if not (media_item.public_status == "published" and media_item.visibility == "public"):
        if media_item.origin != DASHBOARD_UPLOAD_ORIGIN:
            raise HTTPException(status_code=404, detail="Item not found")
        message = db.get(TelegramMessage, media_item.telegram_message_id)
        if message is None or message.chat_id != _upload_chat_id(session_id):
            raise HTTPException(status_code=404, detail="Item not found")
    if media_item.processing_status != "processed":
        raise HTTPException(status_code=400, detail="Source media is not ready yet")
    if not _media_item_is_available(settings, media_item):
        raise HTTPException(status_code=404, detail="Item not found")
    return media_item


def _item_payload(
    db: Session,
    settings: Settings,
    media_item: MediaItem,
    *,
    session_id: str | None = None,
    session_curation: AnonymousSessionCuration | None | object = _ITEM_PAYLOAD_UNSET,
    remix_sources: list[dict[str, Any]] | None = None,
    remix_used_by: list[dict[str, Any]] | None = None,
    private_identity: bool = False,
) -> dict[str, Any]:
    message = media_item.telegram_message
    classification = _latest_loaded_classification(media_item)
    admin_curation = media_item.curation
    if session_curation is _ITEM_PAYLOAD_UNSET:
        session_curation_row = _session_curation(db, session_id, media_item.id, create=False) if session_id else None
    else:
        session_curation_row = session_curation if isinstance(session_curation, AnonymousSessionCuration) else None
    can_see_original = bool(media_item.public_original_allowed)
    if (
        not can_see_original
        and media_item.origin == DASHBOARD_UPLOAD_ORIGIN
        and session_id
        and message
        and message.chat_id == _upload_chat_id(session_id)
    ):
        can_see_original = True

    link_preview = message_link_preview(message)
    remix_tweaks = None
    if media_item.origin == "remix" and message and isinstance(message.raw_metadata_json, dict):
        candidate = message.raw_metadata_json.get("remix_tweaks")
        if isinstance(candidate, dict):
            remix_tweaks = candidate
    return {
        **({"execution_identity": saved_result_identity(
            classification, message.raw_metadata_json if message else None,
        )} if private_identity else {}),
        "id": media_item.id,
        "telegram_message_id": message.telegram_message_id if message else 0,
        "chat_id": message.chat_id if message else "",
        "telegram_post_url": message.telegram_post_url if message else None,
        "date": message.date.isoformat() if message and message.date else None,
        "saved_at": catalog_saved_at(media_item, message).isoformat(),
        "added_at": _share_timestamp(media_item.created_at),
        "caption": message.caption if message and message.caption else "",
        "source_url": link_preview.get("url") or link_preview.get("display_url"),
        "source_display_url": link_preview.get("display_url"),
        "source_site": link_preview.get("site_name"),
        "source_title": link_preview.get("title"),
        "source_description": link_preview.get("description"),
        "source_embed_url": link_preview.get("embed_url"),
        "link_preview": link_preview,
        "grouped_id": message.grouped_id if message else None,
        "forwarded_from": message.forwarded_from if message else None,
        "public_status": media_item.public_status,
        "visibility": media_item.visibility,
        "origin": media_item.origin,
        "media_type": media_item.media_type,
        "mime_type": media_item.mime_type,
        "thumbnail_path": _public_media_url(settings, media_item.thumbnail_path),
        "preview_path": _public_media_url(settings, media_item.preview_path),
        "hover_preview_path": _public_media_url(settings, media_item.hover_preview_path),
        "playback_path": (
            f"/api/items/{media_item.id}/playback/mobile-v{BROWSER_PLAYBACK_VERSION}"
            if can_see_original and media_item.media_type == "video"
            else None
        ),
        "original_path": _public_media_url(settings, media_item.original_path) if can_see_original else None,
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
        "language": _normalize_language(classification.language) if classification else None,
        "meme_format": classification.meme_format if classification else None,
        "tone": classification.tone if classification else [],
        "reaction_type": classification.reaction_type if classification else [],
        "topics": classification.topics_json if classification else [],
        "keywords": classification.keywords_json if classification else [],
        "people_or_characters": classification.people_or_characters_json if classification else [],
        "safety_flags": classification.safety_flags_json if classification else [],
        "search_phrases": classification.search_phrases_json if classification else [],
        "favorite": bool(session_curation_row.favorite) if session_curation_row else False,
        "archived": bool(session_curation_row.archived) if session_curation_row else False,
        "manual_tags": session_curation_row.manual_tags_json if session_curation_row else [],
        "notes": session_curation_row.notes if session_curation_row else "",
        "server_tags": admin_curation.manual_tags_json if admin_curation else [],
        "remix_sources": remix_sources if remix_sources is not None else _remix_sources(db, settings, media_item.id, session_id),
        "remix_used_by": remix_used_by if remix_used_by is not None else _remix_used_by(db, settings, media_item.id, session_id),
        "remix_tweaks": remix_tweaks,
    }


def _latest_loaded_classification(media_item: MediaItem) -> Classification | None:
    if not media_item.classifications:
        return None
    return max(media_item.classifications, key=lambda row: (row.model == "manual", row.created_at, row.id))


def _latest_classification(db: Session, media_item_id: int) -> Classification | None:
    return db.scalar(
        select(Classification)
        .where(Classification.media_item_id == media_item_id)
        .order_by((Classification.model == "manual").desc(), Classification.created_at.desc(), Classification.id.desc())
        .limit(1)
    )


def _session_curation(
    db: Session,
    session_id: str | None,
    media_item_id: int,
    *,
    create: bool,
) -> AnonymousSessionCuration | None:
    if not session_id:
        return None
    row = db.scalar(
        select(AnonymousSessionCuration).where(
            AnonymousSessionCuration.session_id == session_id,
            AnonymousSessionCuration.media_item_id == media_item_id,
        )
    )
    if row is None and create:
        row = AnonymousSessionCuration(
            session_id=session_id,
            media_item_id=media_item_id,
            favorite=False,
            archived=False,
            manual_tags_json=[],
            notes="",
            updated_at=_utc_now(),
        )
        db.add(row)
        db.flush()
    return row


def _session_curation_map(
    db: Session,
    session_id: str,
    media_item_ids: list[int],
) -> dict[int, AnonymousSessionCuration]:
    if not media_item_ids:
        return {}
    rows = db.scalars(
        select(AnonymousSessionCuration).where(
            AnonymousSessionCuration.session_id == session_id,
            AnonymousSessionCuration.media_item_id.in_(media_item_ids),
        )
    ).all()
    return {row.media_item_id: row for row in rows}


def _remix_trace_maps(
    db: Session,
    settings: Settings,
    media_item_ids: list[int],
    *, session_id: str | None = None,
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, list[dict[str, Any]]]]:
    if not media_item_ids:
        return {}, {}
    requested_ids = set(media_item_ids)
    links = db.scalars(
        select(RemixSourceRecord).where(
            or_(
                RemixSourceRecord.remix_media_item_id.in_(media_item_ids),
                RemixSourceRecord.source_media_item_id.in_(media_item_ids),
            )
        )
    ).all()
    if not links:
        return {}, {}

    related_ids = {
        related_id
        for link in links
        for related_id in (link.remix_media_item_id, link.source_media_item_id)
    }
    related_items = db.scalars(
        select(MediaItem)
        .where(MediaItem.id.in_(related_ids))
        .options(
            joinedload(MediaItem.telegram_message),
            selectinload(MediaItem.classifications),
        )
    ).all()
    traces = {item.id: _source_trace_payload_from_item(settings, item) for item in related_items if source_eligible(db, settings, item, session_id)}

    source_links: dict[int, list[RemixSourceRecord]] = {}
    used_by_links: dict[int, list[RemixSourceRecord]] = {}
    for link in links:
        if link.remix_media_item_id in requested_ids:
            source_links.setdefault(link.remix_media_item_id, []).append(link)
        if link.source_media_item_id in requested_ids:
            used_by_links.setdefault(link.source_media_item_id, []).append(link)

    remix_sources = {
        media_item_id: [
            traces.get(link.source_media_item_id, _missing_source_trace(link.source_media_item_id))
            for link in sorted(rows, key=lambda row: (row.position, row.id)) if link.source_media_item_id in traces
        ]
        for media_item_id, rows in source_links.items()
    }
    remix_used_by = {
        media_item_id: [
            traces.get(link.remix_media_item_id, _missing_source_trace(link.remix_media_item_id))
            for link in sorted(rows, key=lambda row: row.id, reverse=True)[:24] if link.remix_media_item_id in traces
        ]
        for media_item_id, rows in used_by_links.items()
    }
    return remix_sources, remix_used_by


def _tags_payload(db: Session) -> dict[str, list[dict[str, Any]]]:
    grouped = (
        select(
            MediaTag.tag_type.label("tag_type"),
            MediaTag.value.label("value"),
            func.count(MediaTag.media_item_id).label("tag_count"),
        )
        .join(PublicSearchDocument, PublicSearchDocument.media_item_id == MediaTag.media_item_id)
        .where(MediaTag.tag_type.in_(PUBLIC_TAG_GROUPS))
        .group_by(MediaTag.tag_type, MediaTag.value)
        .subquery()
    )
    ranked = select(
        grouped.c.tag_type,
        grouped.c.value,
        grouped.c.tag_count,
        func.row_number()
        .over(
            partition_by=grouped.c.tag_type,
            order_by=(grouped.c.tag_count.desc(), grouped.c.value.asc()),
        )
        .label("tag_rank"),
    ).subquery()
    rows = db.execute(
        select(ranked.c.tag_type, ranked.c.value, ranked.c.tag_count)
        .where(ranked.c.tag_rank <= TAG_VALUES_PER_GROUP)
        .order_by(ranked.c.tag_type, ranked.c.tag_rank)
    ).all()
    payload: dict[str, list[dict[str, Any]]] = {tag_type: [] for tag_type in PUBLIC_TAG_GROUPS}
    for tag_type, value, count in rows:
        payload[str(tag_type)].append({"value": str(value), "count": int(count)})
    return payload


def refresh_public_indexes(
    db: Session,
    media_item_ids: list[int] | None = None,
    *,
    settings: Settings | None = None,
) -> dict[str, int]:
    if media_item_ids is not None and not media_item_ids:
        return {"refreshed": 0, "unchanged": 0, "skipped": 0}
    statement = select(MediaItem)
    if media_item_ids is not None:
        statement = statement.where(MediaItem.id.in_(media_item_ids))
    refreshed = unchanged = skipped = 0
    for item in db.scalars(statement).all():
        existing_tag_rows = db.scalars(select(MediaTag).where(MediaTag.media_item_id == item.id)).all()
        document = db.scalar(select(PublicSearchDocument).where(PublicSearchDocument.media_item_id == item.id))
        if not _should_index_public_item(settings, item):
            changed = bool(existing_tag_rows or document)
            if existing_tag_rows:
                db.execute(delete(MediaTag).where(MediaTag.media_item_id == item.id))
            if document is not None:
                db.delete(document)
            if changed:
                refreshed += 1
            skipped += 1
            continue

        classification = _latest_classification(db, item.id)
        message = db.get(TelegramMessage, item.telegram_message_id)
        tags = _tags_for_item(item, classification)
        desired_tags = set(tags)
        existing_tags = {(row.tag_type, row.value, row.source) for row in existing_tag_rows}
        changed = desired_tags != existing_tags
        if changed:
            db.execute(delete(MediaTag).where(MediaTag.media_item_id == item.id))
            for tag_type, value, source in sorted(desired_tags):
                db.add(MediaTag(media_item_id=item.id, tag_type=tag_type, value=value, source=source))
        document_text = _search_document_text(item, message, classification)
        if document is None:
            document = PublicSearchDocument(media_item_id=item.id)
            db.add(document)
            changed = True
        if document.document_text != document_text:
            document.document_text = document_text
            changed = True
        if item.public_status == "published" and item.published_at is None:
            item.published_at = item.created_at or _utc_now()
            changed = True
        if changed:
            document.updated_at = _utc_now()
            refreshed += 1
        else:
            unchanged += 1
    db.flush()
    return {"refreshed": refreshed, "unchanged": unchanged, "skipped": skipped}


def _should_index_public_item(settings: Settings | None, item: MediaItem) -> bool:
    if item.public_status != "published" or item.visibility != "public" or item.processing_status != "processed":
        return False
    return True if settings is None else _media_item_is_available(settings, item)


def _tags_for_item(item: MediaItem, classification: Classification | None) -> list[tuple[str, str, str]]:
    tags: list[tuple[str, str, str]] = [("media_type", item.media_type, "system")]
    if item.duplicate_group_id:
        tags.append(("duplicate_group", item.duplicate_group_id, "system"))
    if not classification:
        return tags
    for tag_type, values in [
        ("topics", classification.topics_json or []),
        ("keywords", classification.keywords_json or []),
        ("tone", classification.tone or []),
        ("reaction_type", classification.reaction_type or []),
    ]:
        tags.extend((tag_type, value, "classification") for value in _clean_tags(values))
    if classification.meme_format:
        tags.append(("meme_format", classification.meme_format, "classification"))
    language = _normalize_language(classification.language)
    if language:
        tags.append(("language", language, "classification"))
    return tags


def _search_document_text(
    item: MediaItem,
    message: TelegramMessage | None,
    classification: Classification | None,
) -> str:
    pieces = [item.media_type, item.mime_type or ""]
    if message:
        pieces.extend([message.caption or "", message.telegram_post_url or ""])
        link_preview = message_link_preview(message)
        pieces.extend(str(link_preview.get(key) or "") for key in ["url", "display_url", "site_name", "title", "description"])
    if classification:
        pieces.extend(
            [
                classification.summary,
                classification.visible_text,
                classification.narration,
                classification.language or "",
                classification.meme_format or "",
                " ".join(classification.tone or []),
                " ".join(classification.reaction_type or []),
                " ".join(classification.topics_json or []),
                " ".join(classification.keywords_json or []),
                " ".join(classification.people_or_characters_json or []),
                " ".join(classification.safety_flags_json or []),
                " ".join(classification.search_phrases_json or []),
            ]
        )
    return "\n".join(piece for piece in pieces if piece).lower()


def _remix_sources(db: Session, settings: Settings, media_item_id: int, session_id: str | None = None) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(RemixSourceRecord)
        .where(RemixSourceRecord.remix_media_item_id == media_item_id)
        .order_by(RemixSourceRecord.position, RemixSourceRecord.id)
    ).all()
    return [_source_trace_payload(db, settings, row.source_media_item_id) for row in rows if source_eligible(db, settings, db.get(MediaItem, row.source_media_item_id), session_id)]


def _remix_used_by(db: Session, settings: Settings, media_item_id: int, session_id: str | None = None) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(RemixSourceRecord)
        .where(RemixSourceRecord.source_media_item_id == media_item_id)
        .order_by(RemixSourceRecord.id.desc())
        .limit(24)
    ).all()
    return [_source_trace_payload(db, settings, row.remix_media_item_id) for row in rows if source_eligible(db, settings, db.get(MediaItem, row.remix_media_item_id), session_id)]


def _source_trace_payload(db: Session, settings: Settings, media_item_id: int) -> dict[str, Any]:
    item = db.scalar(
        select(MediaItem)
        .where(MediaItem.id == media_item_id)
        .options(
            joinedload(MediaItem.telegram_message),
            selectinload(MediaItem.classifications),
        )
    )
    if item is None:
        return _missing_source_trace(media_item_id)
    return _source_trace_payload_from_item(settings, item)


def _source_trace_payload_from_item(settings: Settings, item: MediaItem) -> dict[str, Any]:
    message = item.telegram_message
    classification = _latest_loaded_classification(item)
    link_preview = message_link_preview(message)
    title = classification.summary if classification and classification.summary else (message.caption if message and message.caption else link_preview.get("title") or f"Media {item.id}")
    return {
        "id": item.id,
        "title": title,
        "media_type": item.media_type,
        "thumbnail_path": _public_media_url(settings, item.thumbnail_path),
        "preview_path": _public_media_url(settings, item.preview_path),
        "original_path": _public_media_url(settings, item.original_path) if item.public_original_allowed else None,
    }


def _missing_source_trace(media_item_id: int) -> dict[str, Any]:
    return {
        "id": media_item_id,
        "title": f"Media {media_item_id}",
        "media_type": "unknown",
        "thumbnail_path": None,
        "preview_path": None,
        "original_path": None,
    }


def _job_payload(job: RemixJobRecord) -> dict[str, Any]:
    attempts = sorted(job.attempts, key=lambda attempt: attempt.attempt)
    payload: dict[str, Any] = {
        "id": job.id,
        "left_id": job.left_id,
        "right_id": job.right_id,
        "safety_mode": job.safety_mode,
        "status": job.status,
        "message": job.message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "retry_after_seconds": job.retry_after_seconds,
        "block_reason": job.block_reason,
        "rejection_reason": job.rejection_reason,
        "error": job.error,
        "embedding_error": job.embedding_error,
        "attempts": [
            {
                "attempt": attempt.attempt,
                "mitigation_id": attempt.mitigation_id,
                "label": attempt.label,
                "description": attempt.description,
                "status": attempt.status,
                "message": attempt.message,
                "error": attempt.error,
                "block_reason": attempt.block_reason,
                "rejection_reason": attempt.rejection_reason,
            }
            for attempt in attempts
        ],
        "failure_explanation": (job.result_json or {}).get("failure_explanation"),
        "generation_identity": generation_identity(capture=job.execution_capture_json),
        "suggested_retry": (job.result_json or {}).get("suggested_retry"),
    }
    result = (job.result_json or {}).get("result")
    if result:
        payload["result"] = {**result, "generation_identity": generation_identity(result.get("generation_provenance"))}
        payload["image_url"] = result.get("image_url") or f"/api/remix/jobs/{job.id}/image"
    if job.tweaks_json:
        payload["tweaks"] = job.tweaks_json
    return payload


def _ready_remix_result_or_409(settings: Settings, job: RemixJobRecord) -> RemixResult:
    result_payload = (job.result_json or {}).get("result")
    if not isinstance(result_payload, dict):
        raise HTTPException(status_code=409, detail="Generated remix result is missing")
    try:
        provenance = RemixGenerationProvenance.parse(result_payload.get("generation_provenance"))
    except ValueError:
        raise HTTPException(status_code=409, detail="Generated remix evidence is invalid") from None
    image_path = _remix_job_image_path(settings, job, None)
    if image_path is None:
        raise HTTPException(status_code=409, detail="Generated remix draft image is missing")
    try:
        metadata = ClassificationPayload.model_validate(result_payload)
    except Exception as exc:
        raise HTTPException(status_code=409, detail="Generated remix metadata is invalid") from exc
    return RemixResult(
        job_id=job.id,
        left_id=job.left_id,
        right_id=job.right_id,
        image_path=image_path,
        metadata=metadata,
        raw_response={"source": "online_ready_job"},
        generation_provenance=provenance,
        tweaks=remix_tweaks_from_payload(job.tweaks_json or None),
        safety_mode=str(result_payload.get("safety_mode") or job.safety_mode or "standard"),
    )


def _remix_job_image_path(settings: Settings, job: RemixJobRecord, saved_item: MediaItem | None) -> Path | None:
    candidates: list[str | None] = []
    if saved_item:
        if saved_item.public_original_allowed:
            candidates.append(saved_item.original_path)
        candidates.extend([saved_item.preview_path, saved_item.thumbnail_path])
    candidates.append(((job.result_json or {}).get("result") or {}).get("image_path"))
    for candidate in candidates:
        path = _resolve_remix_image_candidate(settings, candidate)
        if path is not None:
            return path
    return None


def _resolve_remix_image_candidate(settings: Settings, candidate: str | None) -> Path | None:
    if not candidate:
        return None
    raw_candidate = str(candidate)
    normalized = raw_candidate.replace("\\", "/").lstrip("/")
    raw_path = Path(raw_candidate)
    paths: list[Path] = []
    if raw_path.is_absolute():
        paths.append(raw_path)
    else:
        public_roots = (
            ("media/originals/", settings.originals_dir),
            ("media/previews/", settings.previews_dir),
            ("media/thumbnails/", settings.thumbnails_dir),
        )
        for prefix, root in public_roots:
            if normalized.startswith(prefix):
                paths.append(root / normalized.removeprefix(prefix))
                break
        paths.append(settings.remix_drafts_dir / Path(normalized).name)
        paths.append(raw_path)
    for path in paths:
        resolved = path.resolve()
        if resolved.exists() and resolved.is_file():
            return resolved
    return None


def _public_media_url(settings: Settings, stored_path: str | None) -> str | None:
    if not stored_path:
        return None
    path = stored_path.replace("\\", "/").lstrip("/")
    if not _is_public_media_path(path):
        return None
    if not stored_media_file_exists(settings, path):
        return None
    return f"/{path}"


def _is_public_media_path(path: str) -> bool:
    normalized = os.path.normpath(path.replace("\\", "/")).replace("\\", "/")
    if normalized != path.rstrip("/"):
        return False
    if normalized.startswith("../") or "/../" in normalized or normalized.startswith("."):
        return False
    return any(normalized.startswith(root) for root in PUBLIC_STATIC_ROOTS)


def _media_path_allowed(
    db: Session,
    settings: Settings,
    path: str,
    *,
    session_id: str | None,
) -> bool:
    media_item = db.scalar(
        select(MediaItem).where(
            or_(
                MediaItem.original_path == path,
                MediaItem.thumbnail_path == path,
                MediaItem.preview_path == path,
                MediaItem.hover_preview_path == path,
            )
        )
    )
    if media_item is None:
        keyframe = db.scalar(select(MediaKeyframe).where(MediaKeyframe.path == path))
        media_item = keyframe.media_item if keyframe else None
    if media_item is None:
        return False
    if media_item.public_status == "published" and media_item.visibility == "public":
        if path == media_item.original_path and not media_item.public_original_allowed:
            return False
        return _media_item_is_available(settings, media_item)
    if media_item.origin != DASHBOARD_UPLOAD_ORIGIN or not session_id:
        return False
    message = db.get(TelegramMessage, media_item.telegram_message_id)
    return bool(message and message.chat_id == _upload_chat_id(session_id))


def _dashboard_file_or_404(settings: Settings, path: str) -> FileResponse:
    dist = settings.dashboard_dist_dir.resolve()
    normalized = os.path.normpath(path.replace("\\", "/")).replace("\\", "/")
    normalized = normalized.removeprefix("dashboard/dist/")
    if normalized == "dashboard":
        normalized = ""
    if normalized.startswith("../") or normalized.startswith("/") or "/../" in normalized:
        raise HTTPException(status_code=404, detail="Dashboard asset not found")
    candidate = (dist / normalized).resolve()
    if str(candidate).startswith(str(dist)) and candidate.exists() and candidate.is_file():
        return FileResponse(candidate)
    index = dist / "index.html"
    if index.exists() and index.is_file():
        return FileResponse(index)
    raise HTTPException(status_code=404, detail="Dashboard is not built")


def _clean_tags(values: list[str]) -> list[str]:
    seen: set[str] = set()
    tags: list[str] = []
    for raw in values:
        value = str(raw).strip()[:120]
        normalized = " ".join(value.lower().split())
        if not value or normalized in seen:
            continue
        seen.add(normalized)
        tags.append(value)
    return tags[:40]


def _hash_value(secret: str, value: str) -> str:
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def _client_ip_hash(request: Request) -> str:
    runtime: RuntimeState = request.app.state.runtime
    value = _client_address(request)
    return _hash_value(runtime.settings.public_session_secret, value)


def _client_address(request: Request) -> str:
    runtime: RuntimeState = request.app.state.runtime
    if runtime.settings.public_trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if forwarded:
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
    return request.client.host if request.client else ""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _keyset_page(db, statement, model, limit, cursor):
    if cursor:
        try:
            stamp, id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            stamp = datetime.fromisoformat(stamp)
            if model is MediaItem:
                id = int(id)
            elif not isinstance(id, str) or not id or len(id) > 64:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise HTTPException(status_code=400, detail="Invalid cursor") from None
        statement = statement.where(or_(model.created_at < stamp,
                                        and_(model.created_at == stamp, model.id < id)))
    rows = list(db.scalars(statement.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]
    cursor = base64.urlsafe_b64encode(json.dumps([rows[-1].created_at.isoformat(), rows[-1].id]).encode()).decode() if more else None
    return rows, cursor
