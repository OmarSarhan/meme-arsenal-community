"""Community-only preflight; reuse the application's existing settings loader."""

import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url

from meme_arsenal.config import FILE_ENV_MAP, GOOGLE_API_KEY_ALIASES, load_settings


def validate(command=None) -> None:
    command = sys.argv[1:] if command is None else command
    offline = command in (["arsenal", "owner", "inventory"], ["alembic", "upgrade", "head"])
    for name in ("DATABASE_URL_FILE", "PUBLIC_SESSION_SECRET_FILE"):
        if not os.environ.get(name):
            raise ValueError(f"Set {name} to a persistent secret file")
    file_names = set(FILE_ENV_MAP.values()) | {f"{key}_FILE" for key in GOOGLE_API_KEY_ALIASES}
    for name in sorted(file_names):
        if offline and name == "OWNER_SESSION_ID_FILE":
            continue
        if name in os.environ:
            try:
                if not Path(os.environ[name]).read_text().strip():
                    raise OSError
            except (OSError, UnicodeError, ValueError):
                raise ValueError(f"{name} must point to a readable, nonempty secret file") from None
    try:
        settings = load_settings(ignore_owner=offline)
    except Exception:
        # Loader/conversion errors can contain supplied values. Never echo them.
        raise ValueError("Cannot load config/app.env: check setting names, numeric types and file permissions") from None
    if not offline:
        from meme_arsenal.config import validate_owner_id
        validate_owner_id(settings.owner_session_id)
    if settings.app_env != "production":
        raise ValueError("APP_ENV must be production")
    try:
        origin = urlsplit(settings.public_site_url)
        valid_origin = (origin.scheme == "https" and origin.hostname and not origin.username
                        and not origin.password and origin.path in ("", "/")
                        and not origin.query and not origin.fragment)
    except ValueError:
        valid_origin = False
    if not valid_origin:
        raise ValueError("PUBLIC_SITE_URL must be an HTTPS origin for the authenticated ingress")
    if not settings.public_session_cookie_secure:
        raise ValueError("PUBLIC_SESSION_COOKIE_SECURE must be true")
    if len(settings.public_session_secret) < 32 or settings.public_session_secret == "change-me-in-production":
        raise ValueError("PUBLIC_SESSION_SECRET_FILE must contain a non-default secret of at least 32 characters")
    try:
        database = make_url(settings.database_url)
        valid_database = (database.drivername == "postgresql+psycopg" and database.host == "db"
                          and database.port in (None, 5432) and database.database == "meme_arsenal"
                          and database.username == "meme_arsenal" and database.password)
    except Exception:
        valid_database = False
    if not valid_database:
        raise ValueError("DATABASE_URL_FILE must use postgresql+psycopg with user/database meme_arsenal, host db, port 5432 and a password; see init.py")
    from meme_arsenal.online import _validate_online_settings
    _validate_online_settings(settings)
    if settings.reserve_path != Path("/reserve") or not settings.reserve_path.is_dir():
        raise ValueError("RESERVE_PATH must be /reserve with an existing persistent bind directory")
    if not os.access(settings.reserve_path, os.W_OK):
        raise ValueError("Reserve bind directory must be writable by the app image user (UID 0)")
    provider = settings.ai_provider.lower()
    if provider not in ("mock", "google", "openai"):
        raise ValueError("AI_PROVIDER must be mock, google or openai")
    active_providers = {settings.effective_classification_provider}
    if settings.online_embeddings_enabled:
        active_providers.add(settings.effective_embedding_provider)
    if settings.effective_remix_provider == "google":
        active_providers.add("google")
    if "google" in active_providers and not (settings.google_api_key or "").strip():
        raise ValueError("Set GEMINI_API_KEY_FILE for selected Google operations")
    if "openai" in active_providers and not (settings.openai_api_key or "").strip():
        raise ValueError("Set OPENAI_API_KEY_FILE for selected OpenAI operations")
    if os.environ.get("ARSENAL_TELEGRAM_SYNC") == "1":
        if not settings.telegram_api_id or not settings.telegram_api_id.isdecimal():
            raise ValueError("TELEGRAM_API_ID_FILE must contain a numeric Telegram API ID")
        if not settings.telegram_api_hash:
            raise ValueError("Set TELEGRAM_API_HASH_FILE before enabling the telegram profile")
        if not settings.telegram_session_path.is_file():
            raise ValueError("Run the documented interactive telegram-login before starting sync")


if __name__ == "__main__":
    try:
        validate()
    except ValueError as error:
        sys.exit(f"Community configuration error: {error}")
    if sys.argv[1:] == ["--check"]:
        print("Community configuration OK")
    elif len(sys.argv) > 1:
        if sys.argv[1:] == ["arsenal", "owner", "inventory"]:
            os.execvp("arsenal", sys.argv[1:])
        os.execvp("online-entrypoint", ["online-entrypoint", *sys.argv[1:]])
    else:
        sys.exit("Community configuration error: supply an application command or --check")
