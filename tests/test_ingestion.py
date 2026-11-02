import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from meme_arsenal import ingestion
from meme_arsenal.db import ensure_schema, get_engine
from meme_arsenal.ingestion import CommitUncertain, SourceRecord, ingest_local_record
from meme_arsenal.models import MediaItem, PublicMediaShare, SourceIdentity, TelegramMessage


@pytest.fixture
def intake(settings, tmp_path):
    engine = get_engine(settings)
    factory = sessionmaker(engine)
    root = tmp_path / "input"
    root.mkdir()
    def run(key="b.png", namespace="library", **kwargs):
        return ingest_local_record(factory, settings, root, SourceRecord(namespace, key, **kwargs), mode="local")
    yield run, factory, root
    engine.dispose()


def test_identity_snapshot_and_compatibility(intake, settings):
    from PIL import Image
    from meme_arsenal.media import process_media_item
    from meme_arsenal.owner import source_eligible
    from meme_arsenal.telegram_importer import backfill_telegram_post_urls
    run, factory, root = intake
    Image.new("RGB", (20, 20), "red").save(root / "b.png")
    first = run(caption="first")
    assert first.status == "created" and first.media_item_ids == [first.media_item_id]
    assert run(caption="edited").status == "unchanged"
    (root / "a.png").write_bytes((root / "b.png").read_bytes())
    second = run("a.png")
    other = run(namespace="other")
    assert len({first.media_item_id, second.media_item_id, other.media_item_id}) == 3
    assert run().media_item_id == first.media_item_id
    (root / "b.png").write_bytes(b"changed")
    assert run().status == "conflict"
    assert run().media_item_ids == []
    (root / "a.png").rename(root / "renamed.png")
    assert run("renamed.png").status == "created"
    with factory() as db:
        item = db.get(MediaItem, first.media_item_id)
        message = db.get(TelegramMessage, first.message_id)
        assert message.caption == "first" and message.date is None
        assert backfill_telegram_post_urls(db)["backfilled"] == 0
        assert message.telegram_post_url is None
        assert process_media_item(db, settings, item)
        assert source_eligible(db, settings, item, None)
        assert db.scalar(select(func.count()).select_from(PublicMediaShare)) == 0
        assert item.origin == "folder" and item.public_original_allowed
        db.commit()
    (root / "b.png").unlink()
    with factory() as db:
        assert (settings.reserve_path / db.get(MediaItem, first.media_item_id).original_path).is_file()


@pytest.mark.parametrize("key", ["", "/x", "a//b", "a/./b", "../x", "a/", "\0", "x" * 1025, "é" * 513, "\ud800"])
def test_invalid_keys(intake, key):
    run, _, _ = intake
    with pytest.raises((ValueError, UnicodeError)):
        run(key)


def test_symlink_and_owner_and_corruption(intake, settings):
    run, factory, root = intake
    (root / "b.png").write_bytes(b"original")
    (root / "link.png").symlink_to(root / "b.png")
    with pytest.raises(ValueError, match="symlink"):
        run("link.png")
    result = run()
    record = SourceRecord("library", "b.png")
    with pytest.raises(ValueError, match="ownership"):
        ingest_local_record(factory, settings, root, record, mode="hosted", owner_id=settings.owner_session_id)
    with pytest.raises(ValueError, match="configured"):
        ingest_local_record(factory, settings, root, record, mode="hosted", owner_id="foreign")
    with factory() as db:
        media = db.get(MediaItem, result.media_item_id)
        (settings.reserve_path / media.original_path).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="corrupt"):
        run()


@pytest.mark.parametrize("replacement", ["image", "undecodable"])
def test_processing_preserves_snapshot_authority(intake, settings, replacement):
    from PIL import Image
    from meme_arsenal.media import process_media_item
    run, factory, root = intake
    Image.new("RGB", (20, 20), "red").save(root / "b.png")
    result = run()
    with factory() as db:
        item = db.get(MediaItem, result.media_item_id)
        original_identity = (item.sha256, item.size_bytes)
        stored = settings.reserve_path / item.original_path
        if replacement == "image":
            Image.new("RGB", (20, 20), "blue").save(stored)
        else:
            stored.write_bytes(b"not an image")
        processed = process_media_item(db, settings, item)
        db.commit()
    with factory() as db:
        item = db.get(MediaItem, result.media_item_id)
        assert (item.sha256, item.size_bytes) == original_identity
        assert not processed and item.processing_status == "failed"
        assert "snapshot" in item.processing_error
    with pytest.raises(ValueError, match="corrupt"):
        run()
    # Replacing the source too must not legitimize corrupted reserved bytes.
    (root / "b.png").write_bytes(stored.read_bytes())
    with pytest.raises(ValueError, match="corrupt"):
        run()


@pytest.mark.parametrize("failure", ["copy", "flush", "commit_before", "commit_after"])
def test_attempt_failure_ownership(intake, settings, monkeypatch, failure):
    run, factory, root = intake
    (root / "b.png").write_bytes(b"bytes")
    if failure == "copy":
        def broken(source, target):
            target.write_bytes(b"partial")
            raise OSError("copy failed")
        monkeypatch.setattr(ingestion, "_snapshot", broken)
    else:
        original = Session.commit
        def broken_commit(db):
            if failure == "commit_after":
                original(db)
            raise OSError("reply lost")
        if failure.startswith("commit"):
            monkeypatch.setattr(Session, "commit", broken_commit)
        else:
            def fail_flush(db, *_):
                if any(isinstance(row, SourceIdentity) for row in db.new):
                    raise RuntimeError("precommit")
            event.listen(factory, "before_flush", fail_flush)
    with pytest.raises((OSError, RuntimeError, CommitUncertain)):
        run()
    files = list((settings.reserve_path / "originals" / "folder").glob("attempt-*/*"))
    assert bool(files) == failure.startswith("commit")
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(SourceIdentity)) == (failure == "commit_after")
    if failure == "commit_after":
        monkeypatch.undo()
        assert run().status == "unchanged"
        assert files[0].read_bytes() == b"bytes"


def test_source_mutation_and_race_bound(intake, monkeypatch):
    run, factory, root = intake
    (root / "b.png").write_bytes(b"bytes")
    real_signature = ingestion._signature
    calls = []
    def mutated(path):
        calls.append(1)
        return real_signature(path) + (len(calls),)
    monkeypatch.setattr(ingestion, "_signature", mutated)
    with pytest.raises(ValueError, match="changed"):
        run()
    monkeypatch.undo()
    attempts = []
    def race(db, *_):
        if any(isinstance(row, SourceIdentity) for row in db.new):
            attempts.append(1)
            raise IntegrityError("insert", {}, Exception(
                "UNIQUE constraint failed: source_identities.kind, source_identities.namespace, source_identities.external_key"))
    event.listen(factory, "before_flush", race)
    with pytest.raises(RuntimeError, match="three attempts"):
        run()
    assert len(attempts) == 3


@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_commit_retains_snapshot(intake, settings, monkeypatch, committed):
    run, factory, root = intake
    (root / "b.png").write_bytes(b"retained snapshot")
    original = Session.commit
    def interrupted(db):
        if committed:
            original(db)
        raise KeyboardInterrupt
    monkeypatch.setattr(Session, "commit", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run()
    files = list((settings.reserve_path / "originals" / "folder").glob("attempt-*/*"))
    assert len(files) == 1 and files[0].read_bytes() == b"retained snapshot"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(SourceIdentity)) == int(committed)
    monkeypatch.undo()
    assert run().status == ("unchanged" if committed else "created")


def test_schema_init_and_restrictive_fk(intake, settings):
    run, factory, root = intake
    ensure_schema(settings)
    ensure_schema(settings)
    (root / "b.png").write_bytes(b"bytes")
    result = run()
    with factory() as db:
        db.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
        with pytest.raises(IntegrityError):
            db.execute(MediaItem.__table__.delete().where(MediaItem.id == result.media_item_id))


def test_multibyte_limits(intake):
    run, _, root = intake
    (root / "b.png").write_bytes(b"bytes")
    assert run(namespace="é" * 64).status == "created"
    with pytest.raises(ValueError):
        run(namespace="é" * 65)


@pytest.mark.parametrize("damage", ["missing", "deleted", "dangling"])
def test_mapped_damage_is_not_resurrected(intake, settings, damage):
    run, factory, root = intake
    (root / "b.png").write_bytes(b"bytes")
    result = run()
    with factory() as db:
        media = db.get(MediaItem, result.media_item_id)
        if damage == "missing":
            (settings.reserve_path / media.original_path).unlink()
        elif damage == "deleted":
            media.public_status = "deleted"
        else:
            # Legacy SQLite has no global FK enforcement; verify helper invariant too.
            db.execute(MediaItem.__table__.delete().where(MediaItem.id == media.id))
        db.commit()
    with pytest.raises(ValueError):
        run()
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(SourceIdentity)) == 1


def test_unrelated_integrity_error_is_not_retried(intake):
    run, factory, root = intake
    (root / "b.png").write_bytes(b"bytes")
    calls = []
    def fail(db, *_):
        calls.append(1)
        raise IntegrityError("insert", {}, Exception("unrelated constraint"))
    event.listen(factory, "before_flush", fail)
    with pytest.raises(IntegrityError, match="unrelated"):
        run()
    assert len(calls) == 1


def test_postgres_same_key_convergence(postgres_settings, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    settings = postgres_settings
    engine = get_engine(settings)
    factory = sessionmaker(engine)
    root = tmp_path / "input"
    root.mkdir()
    (root / "same.png").write_bytes(b"bytes")
    barrier = Barrier(2)
    existing = ingestion._existing
    def simultaneous(*args):
        result = existing(*args)
        if result is None:
            barrier.wait(timeout=10)
        return result
    monkeypatch.setattr(ingestion, "_existing", simultaneous)
    def run():
        return ingest_local_record(factory, settings, root, SourceRecord("library", "same.png"),
                                   mode="hosted", owner_id=settings.owner_session_id)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: run(), range(2)))
        assert sorted(r.status for r in results) == ["created", "unchanged"]
        assert len({r.media_item_id for r in results}) == 1
        monkeypatch.setattr(ingestion, "_existing", existing)
        def different(namespace):
            return ingest_local_record(factory, settings, root, SourceRecord(namespace, "same.png"),
                                       mode="hosted", owner_id=settings.owner_session_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            distinct = list(pool.map(different, ["one", "two"]))
        assert len({r.media_item_id for r in distinct}) == 2
        with factory() as db:
            assert all(row.owner_id == settings.owner_session_id for row in db.scalars(select(SourceIdentity)))
    finally:
        engine.dispose()


def test_postgres_full_utf8_key_and_transfer(postgres_settings, settings, tmp_path):
    from dataclasses import replace
    from meme_arsenal.migration import migrate_sqlite_to_postgres
    root = tmp_path / "transfer-input"
    key = "/".join(["é" * 100] * 5 + ["x" * 19])
    assert len(key.encode("utf-8")) == 1024
    source = root / key
    source.parent.mkdir(parents=True)
    source.write_bytes(b"transfer snapshot")
    local_engine = get_engine(settings)
    local_factory = sessionmaker(local_engine)
    record = SourceRecord("é" * 64, key)
    first = ingest_local_record(local_factory, settings, root, record, mode="local")
    target = replace(postgres_settings, reserve_path=settings.reserve_path)
    try:
        counts = migrate_sqlite_to_postgres(target, source_sqlite=tmp_path / "arsenal.sqlite", force=True)
        assert counts["source_identities"] == 1
        engine = get_engine(target)
        try:
            factory = sessionmaker(engine)
            rerun = ingest_local_record(factory, target, root, record, mode="local")
            assert rerun.status == "unchanged" and rerun.media_item_id == first.media_item_id
            with pytest.raises(ValueError, match="ownership"):
                ingest_local_record(factory, target, root, record, mode="hosted", owner_id=target.owner_session_id)
            second = ingest_local_record(factory, target, root, SourceRecord("É" * 64, key), mode="local")
            assert second.media_item_id > first.media_item_id
            with factory() as db:
                assert db.get(SourceIdentity, 1).owner_id is None
        finally:
            engine.dispose()
    finally:
        local_engine.dispose()


def test_postgres_fresh_and_013_upgrade(postgres_settings, monkeypatch):
    from pathlib import Path
    from alembic.command import upgrade
    from alembic.config import Config
    from sqlalchemy import inspect, text
    from meme_arsenal.models import Base
    engine = get_engine(postgres_settings)
    repo = Path(__file__).resolve().parents[1]
    config = Config(str(repo / "alembic.ini"))
    config.set_main_option("script_location", str(repo / "migrations"))
    monkeypatch.setattr("meme_arsenal.config.load_settings", lambda **_: postgres_settings)
    try:
        Base.metadata.drop_all(engine)
        upgrade(config, "013_explicit_recipe_grants")
        with Session(engine) as db:
            db.add(TelegramMessage(id=99, telegram_message_id=88, chat_id="legacy"))
            db.commit()
        upgrade(config, "head")
        assert "source_identities" in inspect(engine).get_table_names()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT telegram_message_id FROM telegram_messages WHERE id=99")).scalar_one() == 88
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE alembic_version"))
        upgrade(config, "head")
        assert "source_identities" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
