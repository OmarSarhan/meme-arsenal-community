from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from sqlalchemy import Boolean, Column, DateTime
from sqlalchemy.types import JSON

from meme_arsenal.migration import _coerce_value, _selected_source_columns, _sqlite_tables


def test_migration_coerces_sqlite_values_for_postgres_columns():
    assert _coerce_value('{"topics":["deploy"]}', Column("payload", JSON())) == {"topics": ["deploy"]}
    assert _coerce_value(1, Column("enabled", Boolean())) is True
    assert _coerce_value(0, Column("enabled", Boolean())) is False

    parsed = _coerce_value("2026-01-02T03:04:05", Column("created_at", DateTime()))

    assert parsed == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_migration_reads_source_sqlite_tables(tmp_path):
    sqlite_path = tmp_path / "arsenal.sqlite"
    connection = sqlite3.connect(sqlite_path)
    try:
        connection.execute("CREATE TABLE telegram_messages (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE media_items (id INTEGER PRIMARY KEY)")
        connection.commit()

        assert _sqlite_tables(connection) == {"telegram_messages", "media_items"}
    finally:
        connection.close()


def test_migration_skips_derived_embedding_vector_column():
    target_columns = {
        "id": Column("id"),
        "embedding_json": Column("embedding_json"),
        "embedding_vector": Column("embedding_vector"),
    }

    assert _selected_source_columns(
        "embeddings",
        ["id", "embedding_json", "embedding_vector"],
        target_columns,
    ) == ["id", "embedding_json"]
