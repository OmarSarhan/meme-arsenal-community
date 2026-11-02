from __future__ import annotations

import atexit
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool
from sqlalchemy.schema import CreateColumn

from .config import Settings
from .models import Base
from .schema_015 import additive_columns_015
from .schema_016 import additive_columns_016


_SESSION_RUNTIMES: dict[str, tuple[Engine, sessionmaker]] = {}
_SESSION_RUNTIME_LOCK = Lock()


def get_engine(settings: Settings) -> Engine:
    connect_args: dict[str, Any] = {}
    engine_options: dict[str, Any] = {}
    if settings.is_sqlite:
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = 30
        engine_options["poolclass"] = StaticPool if settings.database_url.endswith(":memory:") else NullPool
    return create_engine(
        settings.database_url,
        future=True,
        connect_args=connect_args,
        pool_pre_ping=not settings.is_sqlite,
        **engine_options,
    )


def ensure_schema(settings: Settings, engine: Engine | None = None) -> None:
    engine = engine or get_engine(settings)
    Base.metadata.create_all(engine)
    if settings.is_sqlite:
        _ensure_sqlite_additive_columns(engine)


def _ensure_sqlite_additive_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    with engine.begin() as connection:
        for table_name, column in (*additive_columns_015(), *additive_columns_016()):
            if table_name in table_names and column.name not in {
                item["name"] for item in inspect(connection).get_columns(table_name)
            }:
                definition = CreateColumn(column).compile(dialect=engine.dialect)
                connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {definition}"))
    if "classifications" in table_names:
        classification_columns = {column["name"] for column in inspector.get_columns("classifications")}
        if "narration" not in classification_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE classifications ADD COLUMN narration TEXT DEFAULT ''"))
    if "telegram_messages" in table_names:
        message_columns = {column["name"] for column in inspector.get_columns("telegram_messages")}
        if "telegram_post_url" not in message_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE telegram_messages ADD COLUMN telegram_post_url TEXT"))
    if "media_items" in table_names:
        media_columns = {column["name"] for column in inspector.get_columns("media_items")}
        if "hover_preview_path" not in media_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE media_items ADD COLUMN hover_preview_path TEXT"))
        media_columns = {column["name"] for column in inspector.get_columns("media_items")}
        sqlite_columns = {
            "public_status": "TEXT DEFAULT 'published'",
            "published_at": "DATETIME",
            "origin": "TEXT DEFAULT 'telegram'",
            "visibility": "TEXT DEFAULT 'public'",
            "public_original_allowed": "BOOLEAN DEFAULT 1",
        }
        for column_name, column_type in sqlite_columns.items():
            if column_name not in media_columns:
                with engine.begin() as connection:
                    connection.execute(text(f"ALTER TABLE media_items ADD COLUMN {column_name} {column_type}"))
                media_columns.add(column_name)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE media_items "
                    "SET published_at = COALESCE(published_at, created_at) "
                    "WHERE public_status = 'published'"
                )
            )
    if "embeddings" in table_names:
        embedding_columns = {column["name"] for column in inspector.get_columns("embeddings")}
        if "embedding_vector" not in embedding_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE embeddings ADD COLUMN embedding_vector TEXT"))
    for table_name in ("remix_jobs", "remix_attempts"):
        if table_name in table_names:
            columns = {column["name"] for column in inspector.get_columns(table_name)}
            if "rejection_reason" not in columns:
                with engine.begin() as connection:
                    connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN rejection_reason TEXT"))


@contextmanager
def session_scope(settings: Settings) -> Iterator[Session]:
    SessionLocal = _session_factory(settings)
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _session_factory(settings: Settings) -> sessionmaker:
    key = settings.database_url
    runtime = _SESSION_RUNTIMES.get(key)
    if runtime is not None:
        return runtime[1]
    with _SESSION_RUNTIME_LOCK:
        runtime = _SESSION_RUNTIMES.get(key)
        if runtime is None:
            engine = get_engine(settings)
            ensure_schema(settings, engine)
            runtime = (engine, sessionmaker(bind=engine, future=True, expire_on_commit=False))
            _SESSION_RUNTIMES[key] = runtime
    return runtime[1]


def dispose_session_engines() -> None:
    with _SESSION_RUNTIME_LOCK:
        runtimes = list(_SESSION_RUNTIMES.values())
        _SESSION_RUNTIMES.clear()
    for engine, _session_local in runtimes:
        engine.dispose()


atexit.register(dispose_session_engines)
