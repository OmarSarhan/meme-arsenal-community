"""Isolated bootstrap checks; no image paths, operator config or services."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from meme_arsenal import bootstrap
from meme_arsenal.config import load_settings


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    for key in list(os.environ):
        monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "assets"
    source.mkdir()
    (source / "index.html").write_text('<script src="/assets/modern.js"></script>')
    (source / "modern.js").write_text("modern")
    return source


@pytest.mark.parametrize("kind", ["missing", "empty", "no-index", "empty-index", "unreadable"])
def test_invalid_assets_before_any_mutation(fixture, monkeypatch, kind):
    source = fixture
    if kind == "missing":
        source = source / "absent"
    elif kind in {"empty", "no-index"}:
        (source / "index.html").unlink()
    elif kind == "empty-index":
        (source / "index.html").write_text("")
    else:
        original = Path.read_bytes

        def denied(path):
            if path == source / "index.html":
                raise PermissionError("synthetic unreadable index")
            return original(path)

        monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises((OSError, ValueError)):
        bootstrap.install_dashboard(source)
    assert not load_settings().reserve_path.exists()


@pytest.mark.parametrize("setting", ["default", "absolute", "relative", "tilde", "yaml", "dotenv", "file", "precedence"])
def test_settings_and_repeat_install(fixture, monkeypatch, setting):
    base = fixture.parent
    if setting == "yaml" or setting == "precedence":
        (base / "config.yaml").write_text("reserve_path: yaml-reserve\n")
    if setting == "dotenv" or setting == "precedence":
        (base / ".env").write_text("RESERVE_PATH=dotenv-reserve\n")
    if setting == "file" or setting == "precedence":
        secret = base / "reserve-path"
        secret.write_text(str(base / "file-reserve"))
        monkeypatch.setenv("RESERVE_PATH_FILE", str(secret))
    values = {"absolute": str(base / "absolute"), "relative": "relative", "tilde": "~/expanded", "precedence": "environment"}
    if setting in values:
        monkeypatch.setenv("RESERVE_PATH", values[setting])
    settings = load_settings()
    target = settings.dashboard_dist_dir.resolve()
    target.mkdir(parents=True)
    sentinels = [settings.reserve_path / name for name in (
        "media/originals/keep", "data/telegram.session", "remix/drafts/keep", "catalog.json", "config.yaml")]
    for path in sentinels:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("preserve")
    for _ in range(2):
        (target / "stale.js").write_text("old")
        (target / ".stale").write_text("old")
        bootstrap.install_dashboard(fixture)
        assert sorted(p.name for p in target.iterdir()) == ["arsenal-config.json", "index.html", "modern.js"]
        assert json.loads((target / "arsenal-config.json").read_text()) == bootstrap.ONLINE_CONFIG
        assert all(p.read_text() == "preserve" for p in sentinels)
        assert not list(target.parent.glob(".dashboard-install-*"))
    assert load_settings() == settings


@pytest.mark.parametrize("fault", ["copy", "config", "publish"])
def test_failures_preserve_old_dist(fixture, monkeypatch, fault):
    target = load_settings().dashboard_dist_dir.resolve()
    target.mkdir(parents=True)
    (target / "old").write_text("keep")
    original_write = Path.write_text
    original_rename = Path.rename

    def fail(*args, **kwargs):
        raise OSError("synthetic initialization failure")

    def write(path, *args, **kwargs):
        return fail() if path.name == "arsenal-config.json" else original_write(path, *args, **kwargs)

    def rename(path, destination):
        return fail() if path.name == "dist" and path != target else original_rename(path, destination)

    if fault == "copy":
        monkeypatch.setattr(bootstrap.shutil, "copytree", fail)
    elif fault == "config":
        monkeypatch.setattr(Path, "write_text", write)
    else:
        monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(OSError):
        bootstrap.install_dashboard(fixture)
    assert (target / "old").read_text() == "keep"
    assert not list(target.parent.glob(".dashboard-install-*"))


@pytest.mark.parametrize("install", ["0", "1"])
@pytest.mark.parametrize("migration_fails", [False, True])
@pytest.mark.parametrize("missing_assets", [False, True])
def test_entrypoint_exec_and_migration_order(fixture, install, migration_fails, missing_assets):
    # Only relocate the packaged asset constant; execute the real shell/helper chain.
    package = fixture.parent / "package/meme_arsenal"
    package.mkdir(parents=True)
    for name in ("__init__.py", "config.py", "branding.py", "bootstrap.py", "style_guidance.py"):
        text = (ROOT / "src/meme_arsenal" / name).read_text()
        (package / name).write_text(text.replace('/app/static-dashboard', str(fixture)))
    binary = fixture.parent / "bin"
    binary.mkdir()
    (binary / "python").symlink_to(sys.executable)
    for name, body in {"alembic": f"echo migration >> order; exit {int(migration_fails)}",
                       "arsenal": "echo server >> order"}.items():
        path = binary / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    env = {"PATH": f"{binary}:/usr/bin:/bin", "HOME": str(fixture.parent),
           "PYTHONPATH": str(package.parent), "PYTHONDONTWRITEBYTECODE": "1",
           "ARSENAL_INSTALL_DASHBOARD": install}
    if missing_assets:
        (fixture / "index.html").unlink()
    result = subprocess.run(["sh", str(ROOT / "docker/online-entrypoint.sh"), "sh", "-c",
                             "alembic upgrade head && exec arsenal serve-online"], env=env, capture_output=True)
    if missing_assets and install == "1":
        assert result.returncode != 0
        assert b"Dashboard initialization failed" in result.stderr
        assert not Path("order").exists()
        assert not load_settings().reserve_path.exists()
        return
    assert bool(result.returncode) == migration_fails, result.stderr
    assert Path("order").read_text().splitlines() == (["migration"] if migration_fails else ["migration", "server"])
    assert load_settings().dashboard_dist_dir.exists() == (install == "1")
