"""Persistence-only checks; old rows are seeded through reflected historical tables."""
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from alembic.command import downgrade, upgrade
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from meme_arsenal.db import ensure_schema, get_engine
from meme_arsenal.migration import _copy_table, _selected_source_columns
from meme_arsenal.models import Base, Classification, Embedding, MediaItem
from meme_arsenal.schema_015 import additive_columns_015


def migration_config(settings, monkeypatch):
    repo = Path(__file__).resolve().parents[1]
    config = Config(str(repo / "alembic.ini"))
    config.set_main_option("script_location", str(repo / "migrations"))
    monkeypatch.setattr("meme_arsenal.config.load_settings", lambda **_: settings)
    return config


def seed_014(engine):
    metadata = sa.MetaData()
    metadata.reflect(engine)
    with engine.begin() as conn:
        for name in ("telegram_messages", "media_items", "classifications", "embeddings"):
            table = metadata.tables[name]
            values = {"id": 71}
            for col in table.columns:
                if col.name == "id" or col.nullable or col.server_default is not None:
                    continue
                if isinstance(col.type, sa.JSON):
                    value = {"legacy": True} if col.name == "raw_response_json" else []
                elif isinstance(col.type, sa.DateTime):
                    value = datetime(2026, 1, 1)
                elif isinstance(col.type, sa.Boolean):
                    value = True
                elif isinstance(col.type, (sa.Integer, sa.Float)):
                    value = 71
                else:
                    value = "legacy"
                values[col.name] = value
            if name == "classifications":
                values.update(summary="legacy", raw_response_json={"legacy": True})
            elif name == "embeddings":
                values.update(embedding_json=[])
            conn.execute(table.insert().values(**values))


def assert_schema(engine):
    inspector = sa.inspect(engine)
    for table, expected in additive_columns_015():
        actual = next(c for c in inspector.get_columns(table) if c["name"] == expected.name)
        assert actual["nullable"] and actual["default"] is None
        assert isinstance(actual["type"], type(expected.type))
        if isinstance(expected.type, sa.String):
            assert actual["type"].length == expected.type.length
    for table, name, columns in (
        ("classifications", "uq_classification_version", ["media_item_id", "model", "prompt_version"]),
        ("embeddings", "uq_embedding", ["media_item_id", "embedding_model", "embedding_source"]),
    ):
        assert next(c for c in inspector.get_unique_constraints(table) if c["name"] == name)["column_names"] == columns


def assert_rows_and_roundtrip(engine):
    with Session(engine) as db:
        classification = db.get(Classification, 71)
        embedding = db.get(Embedding, 71)
        media = db.get(MediaItem, 71)
        assert classification.raw_response_json == {"legacy": True}
        assert classification.summary == "legacy"
        assert embedding.embedding_json == []
        assert media.original_path is None
        assert classification.execution_provider is classification.compatibility_key is None
        assert embedding.execution_provider is embedding.source_text_hash is None
        assert media.derivative_hashes_json is None
        classification.execution_provider = embedding.execution_provider = "openrouter"
        classification.compatibility_key = "a" * 64
        embedding.source_text_hash = "b" * 64
        media.derivative_hashes_json = {"thumbnail": "c" * 64}
        db.commit()
        db.expire_all()
        assert classification.compatibility_key == "a" * 64
        assert embedding.source_text_hash == "b" * 64
        assert classification.execution_provider == embedding.execution_provider == "openrouter"
        assert media.derivative_hashes_json == {"thumbnail": "c" * 64}
    with engine.begin() as conn:
        for model in (Classification, Embedding):
            table = model.__table__
            row = dict(conn.execute(sa.select(table)).mappings().one())
            row["id"] = 72
            with pytest.raises(sa.exc.IntegrityError):
                with conn.begin_nested():
                    conn.execute(table.insert().values(**row))


@pytest.mark.parametrize("via_alembic", [False, True])
def test_populated_014_upgrade(settings, monkeypatch, via_alembic):
    engine = get_engine(settings)
    config = migration_config(settings, monkeypatch)
    try:
        Base.metadata.drop_all(engine)
        upgrade(config, "014_source_identity")
        seed_014(engine)
        if via_alembic:
            upgrade(config, "head")
        else:
            ensure_schema(settings, engine)
            ensure_schema(settings, engine)
        assert_schema(engine)
        assert_rows_and_roundtrip(engine)
        if via_alembic:
            downgrade(config, "014_source_identity")
            for table, col in additive_columns_015():
                assert col.name not in {c["name"] for c in sa.inspect(engine).get_columns(table)}
            with engine.connect() as conn:
                assert conn.execute(sa.text("SELECT summary FROM classifications WHERE id=71")).scalar_one() == "legacy"
    finally:
        engine.dispose()


@pytest.mark.parametrize("via_alembic", [False, True])
def test_fresh_schema(settings, monkeypatch, via_alembic):
    engine = get_engine(settings)
    try:
        Base.metadata.drop_all(engine)
        if via_alembic:
            upgrade(migration_config(settings, monkeypatch), "head")
        else:
            ensure_schema(settings, engine)
        assert_schema(engine)
    finally:
        engine.dispose()


def test_postgres_populated_014_upgrade(postgres_settings, monkeypatch):
    engine = get_engine(postgres_settings)
    try:
        Base.metadata.drop_all(engine)
        config = migration_config(postgres_settings, monkeypatch)
        upgrade(config, "014_source_identity")
        seed_014(engine)
        upgrade(config, "head")
        assert_schema(engine)
        assert_rows_and_roundtrip(engine)
    finally:
        engine.dispose()


@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize("postgres", [False, True])
def test_transfer_identity_fields(settings, request, present, postgres):
    target_settings = request.getfixturevalue("postgres_settings") if postgres else settings
    engine = get_engine(target_settings)
    source = sqlite3.connect(":memory:")
    source.row_factory = sqlite3.Row
    try:
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            for table_name in ("media_items", "classifications", "embeddings"):
                # Minimal target tables isolate binding from unrelated defaults/FKs.
                columns = [sa.Column("id", sa.Integer, primary_key=True)]
                columns += [column for name, column in additive_columns_015() if name == table_name]
                if table_name == "embeddings":
                    columns.append(sa.Column("embedding_vector", sa.Text))
                table = sa.Table(table_name, sa.MetaData(), *columns)
                table.create(conn)
                fields = [c for c in columns if c.name != "id"] if present else []
                ddl = ", ".join(f"{c.name} TEXT" for c in fields)
                source.execute(f"CREATE TABLE {table_name} (id INTEGER PRIMARY KEY{', ' + ddl if ddl else ''})")
                values = [71]
                for col in fields:
                    if isinstance(col.type, sa.JSON):
                        values.append(json.dumps({"thumbnail": "c" * 64}))
                    elif isinstance(col.type, sa.String) and col.type.length:
                        values.append("a" * col.type.length)
                    else:
                        values.append("[1,2]")
                source.execute(f"INSERT INTO {table_name} VALUES ({','.join('?' for _ in values)})", values)
                selected = _selected_source_columns(table_name, ["id"] + [c.name for c in fields], {c.name: c for c in columns})
                assert "embedding_vector" not in selected
                assert _copy_table(source, conn, table) == 1
                row = conn.execute(sa.select(table)).mappings().one()
                for col in columns[1:]:
                    if col.name == "embedding_vector" or not present:
                        assert row[col.name] is None
                    elif isinstance(col.type, sa.JSON):
                        assert row[col.name] == {"thumbnail": "c" * 64}
                    else:
                        assert row[col.name] == "a" * col.type.length
    finally:
        source.close()
        engine.dispose()
