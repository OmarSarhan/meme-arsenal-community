from __future__ import annotations

from pathlib import Path
from dataclasses import replace

import pytest
from sqlalchemy import inspect
from alembic.command import upgrade
from alembic.config import Config

from meme_arsenal import dashboard
from meme_arsenal.config import (
    ENV_MAP, FILE_ENV_MAP, GOOGLE_API_KEY_ALIASES, Settings, ensure_reserve_layout, load_settings,
)
from meme_arsenal.db import ensure_schema, get_engine


@pytest.mark.parametrize("name", ["classification_provider", "embedding_provider", "remix_provider"])
def test_capability_provider_direct_and_replace(name):
    inherited = Settings(ai_provider="OPENAI")
    effective = f"effective_{name}"
    assert getattr(inherited, name) is None
    assert getattr(inherited, effective) == "openai"
    assert getattr(replace(inherited, ai_provider="google"), effective) == "google"
    for blank in (None, "", " \t"):
        assert getattr(Settings(ai_provider="mock", **{name: blank}), effective) == "mock"
    selected = Settings(**{name: " GoOgLe "})
    assert getattr(selected, name) == "google"
    assert getattr(replace(selected, ai_provider="openai"), effective) == "google"
    for invalid in (42, False, [], {}, "synthetic-secret"):
        with pytest.raises(ValueError) as error:
            Settings(**{name: invalid})
        assert str(error.value) == name.upper()
    assert Settings(ai_provider=" GOOGLE ").effective_classification_provider == " google "


@pytest.mark.parametrize("name", ["classification_provider", "embedding_provider", "remix_provider"])
def test_capability_provider_layers(isolated_config, monkeypatch, name):
    env = name.upper()
    (isolated_config / "config.yaml").write_text(f"{name}: google\n")
    assert getattr(load_settings(), name) == "google"
    (isolated_config / "env").write_text(f"{env}=openai\n")
    assert getattr(load_settings(), name) == "openai"
    (isolated_config / ".env").write_text(f"{env}=mock\n")
    monkeypatch.setenv(env, "")
    assert getattr(load_settings(), name) == "mock"
    monkeypatch.setenv(env, "   ")
    assert getattr(load_settings(), name) is None
    monkeypatch.delenv(env)
    selected = isolated_config / "provider"
    selected.write_text(" GOOGLE \n")
    monkeypatch.setenv(f"{env}_FILE", str(selected))
    assert getattr(load_settings(), name) == "google"
    monkeypatch.setenv(env, "openai")
    assert getattr(load_settings(), name) == "openai"


@pytest.mark.parametrize("alias", GOOGLE_API_KEY_ALIASES)
def test_capability_google_key_alias(isolated_config, monkeypatch, alias):
    from meme_arsenal.remix import remix_available
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.setenv("REMIX_PROVIDER", "google")
    key = isolated_config / "key"
    key.write_text("synthetic-google-key\n")
    monkeypatch.setenv(f"{alias}_FILE", str(key))
    settings = load_settings()
    assert settings.google_api_key == "synthetic-google-key"
    assert remix_available(settings)
    assert settings.ai_model == "gpt-5-mini"
    assert settings.embedding_model == "text-embedding-3-small"
    assert settings.embedding_dimensions == 1536
    assert settings.remix_model == "gemini-3.1-flash-image"


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in (*ENV_MAP.values(), *FILE_ENV_MAP.values()):
        monkeypatch.delenv(name, raising=False)
    for name in GOOGLE_API_KEY_ALIASES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)
    return tmp_path


def test_default_reserve_and_database_are_local(isolated_config):
    for settings in (Settings(), load_settings()):
        assert settings.reserve_path == Path("meme-arsenal-reserve")
        ensure_reserve_layout(settings)
        engine = get_engine(settings)
        try:
            ensure_schema(settings, engine)
            assert Path(engine.url.database).resolve() == (
                isolated_config / "meme-arsenal-reserve/data/arsenal.sqlite"
            )
            assert (settings.data_dir / "arsenal.sqlite").is_file()
        finally:
            engine.dispose()


@pytest.mark.parametrize("value, expected", [(None, False), ("false", False), ("true", True), ("0", False), ("1", True)])
def test_public_sharing_is_explicit_opt_in(isolated_config, monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("PUBLIC_SHARING_ENABLED", value)
    assert load_settings().public_sharing_enabled is expected


def test_public_sharing_rejects_invalid_boolean_without_echo(isolated_config, monkeypatch):
    monkeypatch.setenv("PUBLIC_SHARING_ENABLED", "synthetic-secret-invalid")
    with pytest.raises(ValueError, match="PUBLIC_SHARING_ENABLED must be a boolean") as error:
        load_settings()
    assert "synthetic-secret" not in str(error.value)


@pytest.mark.parametrize("settings_factory", [Settings, load_settings])
@pytest.mark.parametrize("builder", [dashboard.build_dashboard, dashboard.build_online_dashboard])
def test_default_dashboard_build_stays_in_callers_reserve(
    isolated_config, monkeypatch, settings_factory, builder,
):
    settings = settings_factory()
    source = isolated_config / "frontend/dashboard"
    (source / "node_modules").mkdir(parents=True)
    monkeypatch.setattr(dashboard, "frontend_dir", lambda: source)
    monkeypatch.setattr(dashboard.shutil, "which", lambda name: "/fixture/npm")

    def npm_build(command, *, cwd, check):
        assert command[:4] == ["/fixture/npm", "run", "build", "--"]
        assert cwd == source and check
        # Resolve --outDir from npm's cwd, as Vite does, without running a build.
        output = cwd / command[command.index("--outDir") + 1]
        output.mkdir(parents=True, exist_ok=True)
        (output / "index.html").write_text("fixture dashboard")

    monkeypatch.setattr(dashboard.subprocess, "run", npm_build)
    result = builder(settings)
    expected = isolated_config / "meme-arsenal-reserve/dashboard/dist"
    assert (expected / "index.html").read_text() == "fixture dashboard"
    assert (expected / "arsenal-config.json").is_file()
    assert Path(result["dist"]).resolve() == expected


@pytest.mark.parametrize("reserve", ["./custom-reserve", "/reserve"])
@pytest.mark.parametrize("source", ["yaml", "dotenv", "environment"])
def test_reserve_override_derives_database(isolated_config, monkeypatch, reserve, source):
    if source == "yaml":
        (isolated_config / "config.yaml").write_text(f"reserve_path: {reserve}\n")
    elif source == "dotenv":
        (isolated_config / ".env").write_text(f"RESERVE_PATH={reserve}\n")
    else:
        monkeypatch.setenv("RESERVE_PATH", reserve)
    settings = load_settings()
    expected = Path(reserve).resolve()
    assert settings.reserve_path == expected
    assert settings.database_url == f"sqlite:///{expected / 'data/arsenal.sqlite'}"


def test_explicit_database_precedence(isolated_config, monkeypatch):
    (isolated_config / "config.yaml").write_text(
        "reserve_path: ./yaml-reserve\ndatabase_url: sqlite:///yaml.sqlite\n"
    )
    assert load_settings().database_url == "sqlite:///yaml.sqlite"
    (isolated_config / ".env").write_text(
        "RESERVE_PATH=./dotenv-reserve\nDATABASE_URL=sqlite:///dotenv.sqlite\n"
    )
    assert load_settings().database_url == "sqlite:///dotenv.sqlite"
    monkeypatch.setenv("RESERVE_PATH", "/reserve")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///environment.sqlite")
    assert load_settings().database_url == "sqlite:///environment.sqlite"
    database_file = isolated_config / "database-url"
    database_file.write_text("sqlite:///file.sqlite\n")
    monkeypatch.setenv("DATABASE_URL_FILE", str(database_file))
    settings = load_settings()
    assert settings.reserve_path == Path("/reserve")
    assert settings.database_url == "sqlite:///file.sqlite"


def test_reserve_layout_is_created(settings):
    ensure_reserve_layout(settings)
    assert settings.originals_dir.exists()
    assert settings.catalog_dir.exists()
    assert settings.dashboard_dist_dir.exists()


def test_load_settings_reads_env_file_and_ai_api_key_alias(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for env_name in (
        "AI_PROVIDER",
        "AI_MODEL",
        "AI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSIONS",
    ):
        monkeypatch.delenv(env_name, raising=False)
    (tmp_path / "env").write_text(
        "\n".join(
            [
                f"RESERVE_PATH={tmp_path / 'reserve'}",
                "AI_PROVIDER=google",
                "AI_MODEL=gemini-3.5-flash",
                "AI_API_KEY=test-google-key",
                "EMBEDDING_MODEL=gemini-embedding-2",
                "EMBEDDING_DIMENSIONS=768",
                "IMPORT_DELAY_SECONDS=0.75",
            ]
        ),
        encoding="utf-8",
    )

    loaded = load_settings()

    assert loaded.ai_provider == "google"
    assert loaded.ai_model == "gemini-3.5-flash"
    assert loaded.google_api_key == "test-google-key"
    assert loaded.embedding_model == "gemini-embedding-2"
    assert loaded.embedding_dimensions == 768
    assert loaded.import_delay_seconds == 0.75


def test_load_settings_reads_file_backed_secrets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for env_name in (
        "DATABASE_URL",
        "DATABASE_URL_FILE",
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_FILE",
        "OPENAI_API_KEY",
        "OPENAI_API_KEY_FILE",
        "TELEGRAM_API_ID",
        "TELEGRAM_API_ID_FILE",
        "TELEGRAM_API_HASH",
        "TELEGRAM_API_HASH_FILE",
        "POSTGRES_PASSWORD",
        "POSTGRES_PASSWORD_FILE",
        "PUBLIC_SESSION_SECRET",
        "PUBLIC_SESSION_SECRET_FILE",
    ):
        monkeypatch.delenv(env_name, raising=False)

    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    values = {
        "database_url": "postgresql+psycopg://meme_arsenal:secret@db:5432/meme_arsenal",
        "gemini": "file-gemini-key",
        "openai": "file-openai-key",
        "telegram_api_id": "123456",
        "telegram_api_hash": "file-telegram-hash",
        "postgres_password": "file-postgres-password",
        "public_session_secret": "file-public-session-secret",
    }
    for name, value in values.items():
        (secret_dir / name).write_text(value, encoding="utf-8")

    monkeypatch.setenv("DATABASE_URL_FILE", str(secret_dir / "database_url"))
    monkeypatch.setenv("GEMINI_API_KEY_FILE", str(secret_dir / "gemini"))
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(secret_dir / "openai"))
    monkeypatch.setenv("TELEGRAM_API_ID_FILE", str(secret_dir / "telegram_api_id"))
    monkeypatch.setenv("TELEGRAM_API_HASH_FILE", str(secret_dir / "telegram_api_hash"))
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", str(secret_dir / "postgres_password"))
    monkeypatch.setenv("PUBLIC_SESSION_SECRET_FILE", str(secret_dir / "public_session_secret"))

    loaded = load_settings()

    assert loaded.database_url == values["database_url"]
    assert loaded.google_api_key == values["gemini"]
    assert loaded.openai_api_key == values["openai"]
    assert loaded.telegram_api_id == values["telegram_api_id"]
    assert loaded.telegram_api_hash == values["telegram_api_hash"]
    assert loaded.postgres_password == values["postgres_password"]
    assert loaded.public_session_secret == values["public_session_secret"]

    monkeypatch.setenv("GEMINI_API_KEY", "direct-gemini-key")
    assert load_settings().google_api_key == "direct-gemini-key"


def test_schema_creates_core_tables(settings):
    engine = get_engine(settings)
    ensure_schema(settings, engine)
    tables = set(inspect(engine).get_table_names())
    assert {
        "telegram_messages",
        "media_items",
        "classifications",
        "embeddings",
        "curation",
        "import_checkpoints",
    }.issubset(tables)
    assert {
        "anonymous_sessions",
        "anonymous_session_curation",
        "media_tags",
        "public_search_documents",
        "remix_jobs",
        "remix_attempts",
        "remix_sources",
        "gemini_call_errors",
    }.issubset(tables)
    media_columns = {column["name"] for column in inspect(engine).get_columns("media_items")}
    assert {
        "public_status",
        "published_at",
        "origin",
        "visibility",
        "public_original_allowed",
    }.issubset(media_columns)
    remix_job_columns = {column["name"] for column in inspect(engine).get_columns("remix_jobs")}
    remix_attempt_columns = {column["name"] for column in inspect(engine).get_columns("remix_attempts")}
    assert "rejection_reason" in remix_job_columns
    assert "rejection_reason" in remix_attempt_columns
    classification_columns = {column["name"] for column in inspect(engine).get_columns("classifications")}
    assert "narration" in classification_columns


def test_alembic_upgrade_head_creates_online_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "alembic.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("RESERVE_PATH", str(tmp_path / "reserve"))
    monkeypatch.setenv("OWNER_SESSION_ID_FILE", str(tmp_path / "lost-owner"))

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    upgrade(config, "head")

    loaded = load_settings(ignore_owner=True)
    engine = get_engine(loaded)
    tables = set(inspect(engine).get_table_names())
    assert {"media_tags", "public_search_documents", "remix_jobs", "gemini_call_errors"}.issubset(tables)
    assert "narration" in {column["name"] for column in inspect(engine).get_columns("classifications")}
    assert "rejection_reason" in {column["name"] for column in inspect(engine).get_columns("remix_jobs")}
    assert "rejection_reason" in {column["name"] for column in inspect(engine).get_columns("remix_attempts")}
