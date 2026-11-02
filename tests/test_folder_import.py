import json
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from meme_arsenal import folder_import as folder
from meme_arsenal.cli import app
from meme_arsenal.config import Settings
from meme_arsenal.ingestion import CommitUncertain


@pytest.fixture
def command(tmp_path, monkeypatch):
    root = tmp_path / "input"
    root.mkdir()
    reserve = tmp_path / "reserve"
    settings = Settings(reserve_path=reserve, database_url=f"sqlite:///{reserve}/data/test.sqlite")
    monkeypatch.setattr(folder, "load_settings", lambda _: settings)
    def run(*args, source_root=None):
        return CliRunner().invoke(app, ["import-folder", str(source_root or root),
                                       "--namespace", "test", *args])
    def report():
        return json.loads(max(settings.run_logs_dir.glob("folder-import-*.json"),
                              key=lambda p: p.stat().st_mtime_ns).read_text())
    return root, settings, run, report


def test_sorted_resume_add_rename_relocate_conflict(command):
    root, settings, run, report = command
    for key in ("b.png", "a.png"):
        (root / key).write_bytes(b"image")
    assert run().exit_code == 0
    first = report()
    assert [item["key"] for item in first["items"]] == ["a.png", "b.png"]
    assert run().exit_code == 0
    assert report()["media_item_ids"] == []
    (root / "a.png").rename(root / "c.png")
    assert run().exit_code == 0
    assert len(report()["media_item_ids"]) == 1
    identities = report()["validated_media_item_ids"]
    relocated = root.with_name("relocated")
    root.rename(relocated)
    assert run(source_root=relocated).exit_code == 0
    assert report()["validated_media_item_ids"] == identities
    assert report()["media_item_ids"] == []
    (relocated / "b.png").write_bytes(b"changed")
    assert run(source_root=relocated).exit_code == 1
    assert "conflict" in [item["status"] for item in report()["items"]]
    assert all(path.stat().st_mode & 0o777 == 0o600
               for path in settings.run_logs_dir.glob("*.json"))


@pytest.mark.parametrize("args", [("--mode", "bad"), ("--namespace", ""),
                                  ("--pipeline-limit", "0")])
def test_empty_validation_no_side_effects(command, args):
    _, settings, run, _ = command
    assert run(*args).exit_code == 2
    assert not settings.reserve_path.exists()


@pytest.mark.parametrize("target", ["reserve", "db", "layout", "symlink", "fifo"])
def test_preflight(command, monkeypatch, target):
    root, settings, run, _ = command
    if target == "reserve":
        settings = replace(settings, reserve_path=root / "out")
    elif target == "db":
        settings = replace(settings, database_url=f"sqlite:///{root}/test.sqlite")
    elif target == "layout":
        settings.reserve_path.mkdir()
        (settings.reserve_path / "media").symlink_to(root, target_is_directory=True)
    elif target == "symlink":
        (root / "link.png").symlink_to(root / "absent")
    else:
        import os
        os.mkfifo(root / "pipe.png")
    monkeypatch.setattr(folder, "load_settings", lambda _: settings)
    assert run().exit_code == 2
    assert not settings.run_logs_dir.exists()


@pytest.mark.parametrize("target", ["thumbnail", "preview", "keyframes", "external"])
def test_derivative_symlinks_rejected_before_writes(command, monkeypatch, target):
    from PIL import Image
    root, settings, run, report = command
    configured = replace(settings, ai_provider="mock", embedding_dimensions=32)
    monkeypatch.setattr(folder, "load_settings", lambda _: configured)
    source = root / "a.png"
    Image.new("RGB", (64, 64), "red").save(source)
    assert run().exit_code == 0
    item_id = report()["media_item_ids"][0]
    stem = f"media-{item_id:08d}"
    if target in ("thumbnail", "preview"):
        directory = settings.thumbnails_dir if target == "thumbnail" else settings.previews_dir
        (directory / f"{stem}.jpg").symlink_to(source)
    elif target == "keyframes":
        (settings.keyframes_dir / stem).symlink_to(root, target_is_directory=True)
    else:
        external = root.with_name("external-keyframes")
        external.mkdir()
        settings.keyframes_dir.rmdir()
        settings.keyframes_dir.symlink_to(external, target_is_directory=True)
        (external / stem).symlink_to(root, target_is_directory=True)
    source_before = source.read_bytes()
    database = settings.data_dir / "test.sqlite"
    database_before = database.read_bytes()
    reports_before = set(settings.run_logs_dir.iterdir())
    calls = []
    pipeline = folder.run_media_pipeline
    def observed_pipeline(*args, **kwargs):
        calls.append(True)
        return pipeline(*args, **kwargs)
    monkeypatch.setattr(folder, "run_media_pipeline", observed_pipeline)
    result = run("--pipeline-limit", "1")
    assert source.read_bytes() == source_before
    assert result.exit_code == 2
    assert database.read_bytes() == database_before
    assert set(settings.run_logs_dir.iterdir()) == reports_before
    assert calls == []


@pytest.mark.parametrize("form", ["uri", "escaped-uri", "ignored-memory-query"])
def test_sqlite_destination_rejected_before_writes(command, monkeypatch, form):
    from urllib.parse import quote
    root, settings, run, _ = command
    database = root / "import database.sqlite"
    filename = quote(str(database), safe="/") if form == "escaped-uri" else str(database)
    url = (f"sqlite:///{filename}?mode=memory" if form == "ignored-memory-query"
           else f"sqlite:///file:{filename}?uri=true")
    configured = replace(settings, database_url=url)
    monkeypatch.setattr(folder, "load_settings", lambda _: configured)
    result = run()
    assert result.exit_code == 2
    assert not database.exists()
    assert not settings.reserve_path.exists()


def test_preflight_allows_unrelated_derivative_symlinks(command):
    root, settings, _, _ = command
    external = root.with_name("external")
    external.mkdir()
    (external / "loop").symlink_to(external, target_is_directory=True)
    (external / "image.jpg").symlink_to(external / "absent.jpg")
    settings.media_dir.mkdir(parents=True)
    settings.thumbnails_dir.symlink_to(external, target_is_directory=True)
    assert folder.preflight(settings, root, "test", "local", 1) == []


@pytest.mark.parametrize("error,expected,count", [(ValueError("SECRET"), 1, 2),
                                                  (CommitUncertain("SECRET"), 1, 1),
                                                  (KeyboardInterrupt(), 130, 1)])
def test_intake_failures(command, monkeypatch, error, expected, count):
    from meme_arsenal import online
    root, _, run, report = command
    for key in ("a.png", "b.png"):
        (root / key).write_bytes(b"x")
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise error
    monkeypatch.setattr(folder, "ingest_local_record", fail)
    monkeypatch.setattr(folder, "run_media_pipeline", lambda *a, **k: pytest.fail("downstream"))
    monkeypatch.setattr(online, "refresh_public_indexes", lambda *a, **k: pytest.fail("refresh"))
    result = run("--pipeline-limit", "1") if count == 1 else run()
    assert result.exit_code == expected
    assert len(calls) == count
    assert "SECRET" not in result.output + json.dumps(report())


@pytest.mark.parametrize("outcome", ["ok", "failure", "pause", "throw", "refresh"])
def test_scope_and_followup(command, monkeypatch, outcome):
    from meme_arsenal import online
    root, _, run, report = command
    for key in ("a.png", "b.png", "c.png"):
        (root / key).write_bytes(b"x")
    assert run().exit_code == 0
    scope = report()["validated_media_item_ids"]
    monkeypatch.setattr(folder, "select_pipeline_media_ids", lambda *a, **k: [999, *scope[1:]])
    calls = []
    def pipeline(settings, **kwargs):
        assert kwargs["media_ids"] == scope[1:2]
        assert kwargs["limit"] is None and kwargs["embed_enabled"]
        if outcome == "throw":
            raise ValueError("SECRET")
        return {"process_failed": int(outcome == "failure"),
                "classification_provider_paused": outcome == "pause", "last_error": "SECRET"}
    def refresh(session, **kwargs):
        calls.append(kwargs["media_item_ids"])
        if outcome == "refresh":
            raise ValueError("SECRET")
    monkeypatch.setattr(folder, "run_media_pipeline", pipeline)
    monkeypatch.setattr(online, "refresh_public_indexes", refresh)
    result = run("--pipeline-limit", "1")
    assert result.exit_code == (0 if outcome == "ok" else 3)
    data = report()
    assert (data["eligible"], data["selected"], data["deferred"]) == (2, 1, 1)
    assert calls == ([] if outcome == "throw" else [scope])
    assert "SECRET" not in result.output + json.dumps(data)


def test_report_failure(command, monkeypatch):
    root, _, run, _ = command
    (root / "a.png").write_bytes(b"x")
    def fail(*args):
        raise OSError("SECRET")
    monkeypatch.setattr(folder, "write_report", fail)
    result = run()
    assert result.exit_code == 3 and "SECRET" not in result.output
    (root / "a.png").write_bytes(b"conflict")
    assert run().exit_code == 1


def test_hosted_empty_requires_provisioned_owner(command, monkeypatch):
    from meme_arsenal.db import get_engine, ensure_schema
    from meme_arsenal.models import AnonymousSession
    from sqlalchemy.orm import Session
    root, settings, run, _ = command
    assert run("--mode", "hosted").exit_code == 2
    configured = replace(settings, owner_session_id="folder-owner")
    monkeypatch.setattr(folder, "load_settings", lambda _: configured)
    engine = get_engine(configured)
    ensure_schema(configured, engine)
    with Session(engine) as session:
        session.add(AnonymousSession(id="folder-owner"))
        session.commit()
    engine.dispose()
    assert run("--mode", "hosted").exit_code == 0
    (root / "a.png").write_bytes(b"image")
    assert run("--mode", "hosted").exit_code == 0


def test_two_image_offline_flow(command, monkeypatch):
    from PIL import Image
    from sqlalchemy import select, func
    from meme_arsenal.db import session_scope
    from meme_arsenal.models import Embedding, PublicSearchDocument
    from meme_arsenal.catalog import export_catalog
    from meme_arsenal.search import search_media
    from meme_arsenal.remix import generate_remix
    from meme_arsenal.remix_execution import capture_execution
    from test_remix import gemini_image_response
    import subprocess
    root, settings, run, report = command
    configured = replace(settings, ai_provider="mock", ai_model="mock-classifier",
                         embedding_provider="mock", embedding_model="mock-embedding",
                         embedding_dimensions=32, remix_provider="google",
                         google_api_key="offline-placeholder", remix_model="gemini-3.1-flash-image")
    monkeypatch.setattr(folder, "load_settings", lambda _: configured)
    def denied(*args, **kwargs):
        raise AssertionError("subprocess forbidden")
    monkeypatch.setattr(subprocess, "Popen", denied)
    for key, color in (("a.png", "red"), ("b.png", "blue")):
        Image.new("RGB", (64, 64), color).save(root / key)
    assert run("--pipeline-limit", "2").exit_code == 0
    data = report()
    assert data["stages"]["processed"] == 2
    assert data["stages"]["classified"] == 2
    assert data["stages"]["embedded"] == 2
    monkeypatch.setattr("meme_arsenal.remix.post_google_model", lambda *a: gemini_image_response())
    with session_scope(configured) as session:
        assert session.scalar(select(func.count()).select_from(Embedding)) == 2
        assert session.scalar(select(func.count()).select_from(PublicSearchDocument)) == 2
        export_catalog(session, configured)
        assert search_media(session, configured, "meme")
        result = generate_remix(session, configured, *data["validated_media_item_ids"],
                                execution_capture=capture_execution(configured, "new_direct"))
        assert result.image_path.is_file()
        assert result.generation_provenance.metadata_origin == "model_json"
    assert run("--pipeline-limit", "1").exit_code == 0
    assert report()["selected"] == 0


def test_empty_selection_never_broadens_and_refresh_failure_preserves_pipeline(command, monkeypatch):
    from meme_arsenal import online
    root, _, run, report = command
    (root / "a.png").write_bytes(b"x")
    monkeypatch.setattr(folder, "select_pipeline_media_ids", lambda *a, **k: [999])
    def pipeline(settings, **kwargs):
        assert kwargs["media_ids"] == []
        return {"process_failed": 1}
    def refresh(*args, **kwargs):
        raise ValueError("SECRET")
    monkeypatch.setattr(folder, "run_media_pipeline", pipeline)
    monkeypatch.setattr(online, "refresh_public_indexes", refresh)
    assert run("--pipeline-limit", "1").exit_code == 3
    assert report()["reasons"] == ["pipeline", "refresh"]


def test_memory_pipeline_rejected_before_layout(command, monkeypatch):
    _, settings, run, _ = command
    monkeypatch.setattr(folder, "load_settings",
                        lambda _: replace(settings, database_url="sqlite:///:memory:"))
    assert run("--pipeline-limit", "1").exit_code == 2
    assert not settings.reserve_path.exists()


def test_irrelevant_pause_after_terminal_work_succeeds(command, monkeypatch):
    from meme_arsenal import online
    root, _, run, _ = command
    (root / "a.png").write_bytes(b"x")
    assert run().exit_code == 0
    selections = iter([[1], []])
    monkeypatch.setattr(folder, "select_pipeline_media_ids", lambda *a, **k: next(selections))
    monkeypatch.setattr(folder, "run_media_pipeline", lambda *a, **k: {
        "classification_provider_paused": True})
    monkeypatch.setattr(online, "refresh_public_indexes", lambda *a, **k: {})
    assert run("--pipeline-limit", "1").exit_code == 0
