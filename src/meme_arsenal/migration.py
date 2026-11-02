from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.types import JSON

from .config import Settings
from .db import get_engine
from .models import Base, MediaItem, TelegramMessage


DERIVED_TABLES = {"media_tags", "public_search_documents"}
DERIVED_COLUMNS = {("embeddings", "embedding_vector")}
CHUNK_SIZE = 500


def migrate_sqlite_to_postgres(
    settings: Settings,
    *,
    source_sqlite: Path,
    promote_static_visible_drafts: bool = False,
    force: bool = False,
    allow_empty_source: bool = False,
) -> dict[str, int]:
    if not settings.is_postgres:
        raise ValueError("migrate-sqlite-to-postgres requires a PostgreSQL DATABASE_URL")
    if not source_sqlite.exists():
        raise FileNotFoundError(source_sqlite)

    target_engine = get_engine(settings)
    source_path = source_sqlite.resolve()
    if source_path.stat().st_size == 0:
        raise ValueError(f"Source SQLite file is empty: {source_path}")
    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    counts: dict[str, int] = {}
    try:
        source_tables = _sqlite_tables(source)
        with target_engine.begin() as target:
            if force and not allow_empty_source:
                source_media_count = _source_count(source, "media_items")
                if source_media_count == 0 and _target_has_rows(target):
                    raise ValueError(
                        "Refusing to run --force with empty source sqlite while target is non-empty. "
                        "This likely indicates an incorrect source path. Re-run with --allow-empty-source if intentional."
                    )
            _ensure_empty_target(target, force=force)
            if force:
                for table in reversed(Base.metadata.sorted_tables):
                    target.execute(table.delete())
            for table in Base.metadata.sorted_tables:
                if table.name in DERIVED_TABLES or table.name not in source_tables:
                    continue
                copied = _copy_table(source, target, table)
                counts[table.name] = copied
            _reset_sequences(target)
        if promote_static_visible_drafts:
            counts["promoted_dashboard_uploads"] = _promote_dashboard_uploads(settings)
    finally:
        source.close()
        target_engine.dispose()
    return counts


def _target_has_rows(connection: Any) -> bool:
    for table in Base.metadata.sorted_tables:
        if table.name in DERIVED_TABLES:
            continue
        count = connection.execute(select(func.count()).select_from(table)).scalar_one()
        if count:
            return True
    return False


def _source_count(connection: sqlite3.Connection, table_name: str) -> int:
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {_quote_identifier(table_name)}").fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def _sqlite_tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows}


def _ensure_empty_target(connection: Any, *, force: bool) -> None:
    non_empty: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name in DERIVED_TABLES:
            continue
        count = connection.execute(select(func.count()).select_from(table)).scalar_one()
        if count:
            non_empty.append(table.name)
    if non_empty and not force:
        raise ValueError(f"PostgreSQL target is not empty: {', '.join(non_empty)}")


def _copy_table(source: sqlite3.Connection, target: Any, table: Any) -> int:
    target_columns = {column.name: column for column in table.columns}
    source_columns = [
        str(row["name"])
        for row in source.execute(f"PRAGMA table_info({_quote_identifier(table.name)})").fetchall()
    ]
    selected_columns = _selected_source_columns(table.name, source_columns, target_columns)
    if not selected_columns:
        return 0
    column_sql = ", ".join(_quote_identifier(name) for name in selected_columns)
    rows = source.execute(f"SELECT {column_sql} FROM {_quote_identifier(table.name)}").fetchall()
    batch: list[dict[str, Any]] = []
    copied = 0
    for row in rows:
        batch.append(
            {
                name: _coerce_value(row[name], target_columns[name])
                for name in selected_columns
            }
        )
        if len(batch) >= CHUNK_SIZE:
            target.execute(table.insert(), batch)
            copied += len(batch)
            batch = []
    if batch:
        target.execute(table.insert(), batch)
        copied += len(batch)
    return copied


def _coerce_value(value: Any, column: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, JSON):
        if isinstance(value, str):
            return json.loads(value)
        return value
    if isinstance(column.type, Boolean):
        return bool(value)
    if isinstance(column.type, DateTime) and isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return value


def _selected_source_columns(
    table_name: str, source_columns: list[str], target_columns: dict[str, Any]
) -> list[str]:
    return [
        name
        for name in source_columns
        if name in target_columns and (table_name, name) not in DERIVED_COLUMNS
    ]


def _reset_sequences(connection: Any) -> None:
    for table in Base.metadata.sorted_tables:
        primary_keys = list(table.primary_key.columns)
        if len(primary_keys) != 1 or not isinstance(primary_keys[0].type, Integer):
            continue
        pk = primary_keys[0]
        max_id = connection.execute(select(func.max(pk))).scalar()
        if max_id:
            connection.execute(
                text(
                    """
                    SELECT setval(sequence_name, :value, true)
                    FROM (
                        SELECT pg_get_serial_sequence(:table_name, :column_name) AS sequence_name
                    ) AS seq
                    WHERE sequence_name IS NOT NULL
                    """
                ),
                {"table_name": table.name, "column_name": pk.name, "value": int(max_id)},
            )


def _promote_dashboard_uploads(settings: Settings) -> int:
    engine = get_engine(settings)
    promoted = 0
    with Session(engine) as session:
        rows = session.scalars(
            select(MediaItem)
            .join(TelegramMessage)
            .where(
                MediaItem.origin == "dashboard_upload",
                MediaItem.public_status == "draft",
                MediaItem.visibility == "private",
                MediaItem.processing_status == "processed",
                MediaItem.media_type.in_(["image", "video", "gif"]),
            )
        ).all()
        for item in rows:
            if not item.original_path or not (settings.reserve_path / item.original_path).exists():
                continue
            item.public_status = "published"
            item.visibility = "public"
            item.public_original_allowed = True
            item.published_at = item.published_at or datetime.now(timezone.utc)
            promoted += 1
        session.commit()
    engine.dispose()
    return promoted


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
