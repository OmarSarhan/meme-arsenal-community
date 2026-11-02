"""Offline checks only: never start services or load operator configuration."""

import importlib.util
import os
from pathlib import Path
import stat
import signal
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
# Synthetic syntax fixture; runtime CI generates and authenticates a real Caddy hash.
HASH = "$2a$14$" + "a" * 53


@pytest.fixture
def auth_hash_file(tmp_path):
    path = tmp_path / "owner.hash"
    path.write_text(HASH + "\n")
    return path


def module_at(relative):
    spec = importlib.util.spec_from_file_location("community", ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_readiness_waits_for_installed_term_handler(tmp_path):
    # Model startup before the worker CLI installs its handler; the early TERM
    # is ignored. The CI probe must wait for the handler, not merely a live PID.
    child = subprocess.Popen([sys.executable, "-c", """
import pathlib, signal, sys, time
root = pathlib.Path(sys.argv[1])
signal.signal(signal.SIGTERM, signal.SIG_IGN)
(root / 'starting').touch()
while not (root / 'release').exists():
    time.sleep(.01)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(.01)
""", str(tmp_path)])
    probe = None
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "starting").exists():
            assert child.poll() is None and time.monotonic() < deadline
            time.sleep(.01)
        child.send_signal(signal.SIGTERM)
        code = module_at("scripts/verify_community.py").WORKER_READY_CHECK
        probe = subprocess.Popen([sys.executable, "-c", code, str(child.pid)], stdout=subprocess.PIPE, text=True)
        time.sleep(.2)
        assert probe.poll() is None and child.poll() is None
        (tmp_path / "release").touch()
        stdout, _ = probe.communicate(timeout=5)
        assert probe.returncode == 0 and "TERM handler ready" in stdout
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=5) == 0
    finally:
        if probe is not None and probe.poll() is None:
            probe.kill()
            probe.wait()
        if child.poll() is None:
            child.kill()
            child.wait()


def test_initializer_private_persistent_material(tmp_path, auth_hash_file):
    initializer = module_at("deploy/community/init.py")
    root = tmp_path / "instance"
    old_umask = os.umask(0o077)
    try:
        initializer.initialize(root, "https://Test.invalid:443/", "community:test", "owner", auth_hash_file)
    finally:
        os.umask(old_umask)
    for path in root.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    password = (root / "secrets/postgres_password").read_text().strip()
    database = (root / "secrets/database_url").read_text()
    assert f":{password}@db:5432/meme_arsenal" in database
    assert len((root / "secrets/public_session_secret").read_text().strip()) >= 32
    assert "TELEGRAM_API_ID_FILE=" not in (root / "config/app.env").read_text()
    assert not (root / "secrets/telegram_api_id").exists()
    assert (root / "config/ingress/owner.caddy").read_text() == f"owner {HASH}\n"
    assert "PUBLIC_SITE_URL=https://test.invalid\n" in (root / "config/app.env").read_text()
    caddy = (root / "config/ingress/Caddyfile").read_text()
    assert "https://test.invalid {" in caddy and "SITE_AUTHORITY" not in caddy
    for relative in ("config/app.env", "config/compose.env", "config/ingress/Caddyfile"):
        assert HASH not in (root / relative).read_text()
    with pytest.raises(ValueError, match="already exists"):
        initializer.initialize(root, "https://test.invalid", "community:test", "owner", auth_hash_file)
    assert (root / "secrets/database_url").read_text() == database


def test_initializer_optional_share_site_stays_disabled(tmp_path, auth_hash_file):
    root = tmp_path / "optional"
    module_at("deploy/community/init.py").initialize(root, "https://private.example.test", "test:1", "owner", auth_hash_file, "https://Share.example.test:443/")
    env = (root / "config/app.env").read_text()
    assert "PUBLIC_SHARING_ENABLED=false" in env
    assert "PUBLIC_SHARE_URL=https://share.example.test\n" in env
    caddy = (root / "config/ingress/Caddyfile").read_text()
    assert "https://share.example.test {" in caddy
    assert "header_up -Cookie" in caddy and "header_up Host share.example.test" in caddy
    assert "method HEAD" in caddy and "(/(media|poster))?$" in caddy


@pytest.mark.parametrize("path", [Path("relative"), ROOT / "new-instance"])
def test_initializer_refuses_unsafe_paths(path, auth_hash_file):
    with pytest.raises(ValueError, match="absolute and outside"):
        module_at("deploy/community/init.py").initialize(path, "https://test.invalid", "test:1", "owner", auth_hash_file)


@pytest.mark.parametrize("origin", [
    "http://test.invalid", "https://test.invalid:8443", "https://test.invalid:bad",
    "https://test.invalid:", "https://test.invalid/path", "https://test.invalid?",
    "https://test.invalid#", "https://user:password@test.invalid", "https://127.0.0.1",
    "https://[::1]", "https://test.invalid\n", "https://test.invalid/\n",
    "https://test.invalid{", "https://test.invalid$", "https://test.invalid\\evil",
    "https://test.invalid;", "https://test.invalid'", 'https://test.invalid"',
    "https://*.invalid", "https://-test.invalid", "https://test..invalid",
    "https://test_.invalid", "https://test.invalid.", "https://tést.invalid",
    "https://" + "a" * 64 + ".invalid", "https://" + ("a" * 63 + ".") * 4 + "invalid",
])
def test_initializer_rejects_bad_origins_before_creating(tmp_path, auth_hash_file, origin):
    root = tmp_path / "instance"
    with pytest.raises(ValueError, match="HTTPS DNS origin") as error:
        module_at("deploy/community/init.py").initialize(root, origin, "test:1", "owner", auth_hash_file)
    assert origin not in str(error.value)
    assert not root.exists()


@pytest.mark.parametrize("user", ["", "a b", "a:b", "a\nb", "{owner}", "$owner", "#owner", "a" * 65, "é", "import"])
def test_initializer_rejects_bad_user(tmp_path, auth_hash_file, user):
    root = tmp_path / "instance"
    with pytest.raises(ValueError, match="--auth-user"):
        module_at("deploy/community/init.py").initialize(root, "https://test.invalid", "test:1", user, auth_hash_file)
    assert not root.exists()


@pytest.mark.parametrize("content", [
    b"", b"sensitive-plaintext", HASH.encode() + b"\n\n", (HASH + "\n" + HASH).encode(),
    HASH.replace("14", "03").encode(), HASH.replace("14", "32").encode(),
    HASH.replace("2a", "2x").encode(), HASH[:-1].encode(), HASH.encode() + b"\r\n",
    b"\xff", b" " + HASH.encode(),
])
def test_initializer_rejects_bad_hash_without_echo(tmp_path, auth_hash_file, content):
    auth_hash_file.write_bytes(content)
    root = tmp_path / "instance"
    with pytest.raises(ValueError, match="--auth-hash-file") as error:
        module_at("deploy/community/init.py").initialize(root, "https://test.invalid", "test:1", "owner", auth_hash_file)
    assert HASH not in str(error.value) and "sensitive-plaintext" not in str(error.value)
    assert not root.exists()


def test_initializer_missing_hash_is_redacted(tmp_path):
    with pytest.raises(ValueError, match="--auth-hash-file") as error:
        module_at("deploy/community/init.py").initialize(
            tmp_path / "instance", "https://test.invalid", "test:1", "owner", tmp_path / "sensitive-path")
    assert "sensitive-path" not in str(error.value)
    assert not (tmp_path / "instance").exists()


@pytest.fixture
def preflight(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        monkeypatch.delenv(name)
    module = module_at("docker/community-entrypoint.py")
    for name, value in {
        "OWNER_SESSION_ID": "fixture-owner",
        "DATABASE_URL": "postgresql+psycopg://meme_arsenal:synthetic@db:5432/meme_arsenal",
        "PUBLIC_SESSION_SECRET": "synthetic-fixture-secret-not-for-use-12345",
    }.items():
        path = tmp_path / name
        path.write_text(value)
        monkeypatch.setenv(f"{name}_FILE", str(path))
    for name, value in {
        "APP_ENV": "production", "PUBLIC_SITE_URL": "https://test.invalid",
        "RESERVE_PATH": "/reserve", "AI_PROVIDER": "mock",
    }.items():
        monkeypatch.setenv(name, value)
    # Only substitute the container mount existence/access; real files stay in tmp_path.
    is_dir = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda p: p == Path("/reserve") or is_dir(p))
    monkeypatch.setattr(module.os, "access", lambda p, mode: p == Path("/reserve"))
    return module


def test_upload_first_needs_no_telegram(preflight):
    preflight.validate()


def test_inventory_exempts_only_owner_config(preflight, monkeypatch, tmp_path):
    monkeypatch.setenv("OWNER_SESSION_ID_FILE", str(tmp_path / "lost-owner"))
    with pytest.raises(ValueError, match="OWNER_SESSION_ID_FILE"):
        preflight.validate(["arsenal", "online-worker"])
    preflight.validate(["arsenal", "owner", "inventory"])
    preflight.validate(["alembic", "upgrade", "head"])
    monkeypatch.setenv("PUBLIC_SESSION_SECRET_FILE", str(tmp_path / "lost-signing"))
    with pytest.raises(ValueError, match="PUBLIC_SESSION_SECRET_FILE"):
        preflight.validate(["arsenal", "owner", "inventory"])


@pytest.mark.parametrize("name", ["DATABASE_URL_FILE", "PUBLIC_SESSION_SECRET_FILE"])
def test_required_secret_missing(preflight, monkeypatch, name):
    monkeypatch.delenv(name)
    with pytest.raises(ValueError, match=name):
        preflight.validate()


def test_optional_secret_missing_is_actionable(preflight, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY_FILE", "/nonexistent/sensitive-value")
    with pytest.raises(ValueError, match="GEMINI_API_KEY_FILE") as error:
        preflight.validate()
    assert "sensitive-value" not in str(error.value)


def test_bad_setting_does_not_echo_value(preflight, monkeypatch):
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "sensitive-value")
    with pytest.raises(ValueError, match="numeric types") as error:
        preflight.validate()
    assert "sensitive-value" not in str(error.value)


def test_bad_database_does_not_echo_value(preflight):
    Path(os.environ["DATABASE_URL_FILE"]).write_text("postgresql://sensitive-value")
    with pytest.raises(ValueError, match="DATABASE_URL_FILE") as error:
        preflight.validate()
    assert "sensitive-value" not in str(error.value)


def test_sync_requires_explicit_credentials_then_login(preflight, monkeypatch):
    monkeypatch.setenv("ARSENAL_TELEGRAM_SYNC", "1")
    with pytest.raises(ValueError, match="TELEGRAM_API_ID_FILE"):
        preflight.validate()
    monkeypatch.setenv("TELEGRAM_API_ID", "123456")
    monkeypatch.setenv("TELEGRAM_API_HASH", "synthetic")
    with pytest.raises(ValueError, match="telegram-login"):
        preflight.validate()


def test_selected_provider_requires_key(preflight, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "google")
    with pytest.raises(ValueError, match="GEMINI_API_KEY_FILE"):
        preflight.validate()


@pytest.mark.parametrize("name", ["CLASSIFICATION_PROVIDER", "EMBEDDING_PROVIDER", "REMIX_PROVIDER"])
def test_active_capability_keys(preflight, monkeypatch, name):
    monkeypatch.setenv(name, "google")
    if name == "EMBEDDING_PROVIDER":
        preflight.validate()  # Disabled online embeddings need no key.
        monkeypatch.setenv("ONLINE_EMBEDDINGS_ENABLED", "true")
    with pytest.raises(ValueError, match="GEMINI_API_KEY_FILE"):
        preflight.validate()
    monkeypatch.setenv("AI_API_KEY", "synthetic-key")
    preflight.validate()


def test_unused_global_credentials_and_broken_files(preflight, monkeypatch, tmp_path):
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.setenv("CLASSIFICATION_PROVIDER", "mock")
    monkeypatch.setenv("REMIX_PROVIDER", "openai")
    preflight.validate()
    monkeypatch.setenv("EMBEDDING_PROVIDER_FILE", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="EMBEDDING_PROVIDER_FILE"):
        preflight.validate()
    monkeypatch.delenv("EMBEDDING_PROVIDER_FILE")
    monkeypatch.setenv("AI_PROVIDER", "unknown")
    with pytest.raises(ValueError, match="AI_PROVIDER"):
        preflight.validate()
