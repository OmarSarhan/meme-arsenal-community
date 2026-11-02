"""Populated historical upgrade and transfer compatibility, without inference."""

from datetime import datetime
import json
import sqlite3

from alembic.command import upgrade
import pytest
import sqlalchemy as sa

from meme_arsenal.db import ensure_schema, get_engine
from meme_arsenal.models import Base
from meme_arsenal.migration import _copy_table
from meme_arsenal.schema_016 import additive_columns_016
from test_schema015 import migration_config
from test_remix_execution import capture


def populated_upgrade(settings, monkeypatch, via_alembic):
    engine = get_engine(settings)
    try:
        Base.metadata.drop_all(engine)
        config = migration_config(settings, monkeypatch)
        upgrade(config, "015_execution_identity")
        metadata = sa.MetaData()
        metadata.reflect(engine)
        with engine.begin() as conn:
            for name in ("telegram_messages", "media_items", "remix_jobs", "remix_attempts"):
                table = metadata.tables[name]
                values = {}
                for col in table.columns:
                    if col.nullable or col.server_default is not None:
                        continue
                    if isinstance(col.type, sa.JSON):
                        value = {"legacy": True} if col.name == "result_json" else {}
                    elif isinstance(col.type, sa.DateTime):
                        value = datetime(2026, 1, 1)
                    elif isinstance(col.type, sa.Boolean):
                        value = True
                    elif isinstance(col.type, (sa.Integer, sa.Float)):
                        value = 1
                    else:
                        value = "legacy"
                    values[col.name] = value
                if name == "remix_attempts":
                    values["job_id"] = "legacy"
                if name == "remix_jobs":
                    values["result_json"] = {"legacy": True}
                    values["status"] = "queued"
                conn.execute(table.insert().values(**values))
                if name == "remix_jobs":
                    for status in ("ready", "saved"):
                        conn.execute(
                            table.insert().values(**{**values, "id": status, "status": status})
                        )
        if via_alembic:
            upgrade(config, "head")
        else:
            ensure_schema(settings, engine)
            ensure_schema(settings, engine)
        for table, col in additive_columns_016():
            actual = next(c for c in sa.inspect(engine).get_columns(table) if c["name"] == col.name)
            assert actual["nullable"] and actual["default"] is None
        jobs = sa.Table("remix_jobs", sa.MetaData(), autoload_with=engine)
        with engine.begin() as conn:
            rows = conn.execute(sa.select(jobs)).mappings().all()
            assert {row["status"] for row in rows} == {"queued", "ready", "saved"}
            assert all(row["execution_capture_json"] is None for row in rows)
            assert all(row["execution_first_captured_attempt"] is None for row in rows)
            row = next(row for row in rows if row["id"] == "legacy")
            assert row["result_json"] == {"legacy": True}
            assert row["execution_capture_json"] is None
            assert row["execution_first_captured_attempt"] is None
            assert conn.execute(sa.text("SELECT attempt FROM remix_attempts")).scalar_one() == 1
            conn.execute(
                jobs.update()
                .where(jobs.c.id == "legacy")
                .values(
                    execution_capture_json=capture().payload(), execution_first_captured_attempt=1
                )
            )
            row = conn.execute(sa.select(jobs).where(jobs.c.id == "legacy")).mappings().one()
            assert row["execution_capture_json"] == capture().payload()
            assert row["execution_first_captured_attempt"] == 1
        if settings.is_sqlite:
            # Backup/restore preserves nullable additions and existing result JSON.
            from sqlalchemy.engine import make_url

            source = sqlite3.connect(make_url(settings.database_url).database)
            restored = sqlite3.connect(":memory:")
            try:
                source.backup(restored)
                row = restored.execute(
                    "SELECT result_json, execution_capture_json, execution_first_captured_attempt FROM remix_jobs WHERE id='legacy'"
                ).fetchone()
                assert json.loads(row[0]) == {"legacy": True}
                assert json.loads(row[1]) == capture().payload()
                assert row[2] == 1
            finally:
                source.close()
                restored.close()
    finally:
        engine.dispose()


@pytest.mark.parametrize("via_alembic", [False, True])
def test_populated_015(settings, monkeypatch, via_alembic):
    populated_upgrade(settings, monkeypatch, via_alembic)


def test_postgres_populated_015(postgres_settings, monkeypatch):
    populated_upgrade(postgres_settings, monkeypatch, True)


@pytest.mark.parametrize("present", [False, True])
def test_capture_transfer(settings, present):
    engine = get_engine(settings)
    source = sqlite3.connect(":memory:")
    source.row_factory = sqlite3.Row
    try:
        table = sa.Table(
            "remix_jobs",
            sa.MetaData(),
            sa.Column("id", sa.String, primary_key=True),
            *(col for _, col in additive_columns_016()),
        )
        with engine.begin() as conn:
            Base.metadata.drop_all(conn)
            table.create(conn)
            extra = (
                ", execution_capture_json TEXT, execution_first_captured_attempt INTEGER"
                if present
                else ""
            )
            source.execute("CREATE TABLE remix_jobs (id TEXT PRIMARY KEY" + extra + ")")
            if present:
                source.execute(
                    "INSERT INTO remix_jobs VALUES (?, ?, ?)",
                    ("legacy", json.dumps(capture().payload()), 1),
                )
            else:
                source.execute("INSERT INTO remix_jobs VALUES ('legacy')")
            assert _copy_table(source, conn, table) == 1
            row = conn.execute(sa.select(table)).mappings().one()
            assert row["execution_capture_json"] == (capture().payload() if present else None)
            assert row["execution_first_captured_attempt"] == (1 if present else None)
    finally:
        source.close()
        engine.dispose()
