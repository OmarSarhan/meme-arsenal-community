from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from .branding import RESERVE_DIR_NAME
from .style_guidance import normalize_style_guidance


DEFAULT_RESERVE_PATH = Path(RESERVE_DIR_NAME)
DEFAULT_DATABASE_URL = f"sqlite:///{DEFAULT_RESERVE_PATH / 'data' / 'arsenal.sqlite'}"


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    owner_session_id: str | None = None
    app_env: str = "development"
    reserve_path: Path = DEFAULT_RESERVE_PATH
    database_url: str = DEFAULT_DATABASE_URL
    telegram_api_id: str | None = None
    telegram_api_hash: str | None = None
    google_api_key: str | None = None
    openai_api_key: str | None = None
    postgres_password: str | None = None
    ai_provider: str = "mock"
    ai_model: str = "gpt-5-mini"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    online_embeddings_enabled: bool = False
    classification_prompt_version: str = "v1"
    classification_style_guidance: str = ""
    remix_style_guidance: str = ""
    ai_request_delay_seconds: float | None = None
    ai_max_retries: int = 3
    ai_max_retry_delay_seconds: float = 90.0
    ai_stop_on_rate_limit: bool = True
    ai_credit_probe_interval_seconds: float = 21600.0
    ai_credit_probe_lease_seconds: float = 300.0
    ai_recovery_batch_size: int = 5
    ai_include_video_original: bool = True
    ai_video_inline_max_mb: float = 20.0
    import_batch_size: int = 100
    import_delay_seconds: float = 0.35
    max_video_keyframes: int = 8
    semantic_search_top_k: int = 25
    remix_model: str = "gemini-3.1-flash-image"
    remix_aspect_ratio: str = "1:1"
    remix_image_size: str = "1K"
    remix_include_video_original: bool = True
    remix_video_inline_max_mb: float = 20.0
    redis_url: str = "redis://redis:6379/0"
    public_site_url: str = "http://localhost:8123"
    public_share_url: str = ""
    public_sharing_enabled: bool = False
    public_session_secret: str = "change-me-in-production"
    public_session_cookie_name: str = "arsenal_public_session"
    public_session_cookie_secure: bool = True
    public_session_days: int = 180
    public_trust_proxy_headers: bool = False
    public_items_default_limit: int = 96
    public_items_max_limit: int = 240
    public_remix_jobs_per_hour: int = 16
    public_remix_analyses_per_hour: int = 16
    public_write_actions_per_minute: int = 60
    public_max_active_remix_jobs: int = 2
    public_upload_max_mb: float = 128.0
    browser_playback_backfill_batch_size: int = 4
    remix_job_stale_seconds: int = 3600
    log_json: bool = False

    classification_provider: str | None = None
    embedding_provider: str | None = None
    remix_provider: str | None = None

    def __post_init__(self) -> None:
        for name in ("classification_style_guidance", "remix_style_guidance"):
            object.__setattr__(self, name, normalize_style_guidance(getattr(self, name), name))
        for name in ("classification_provider", "embedding_provider", "remix_provider"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError(name.upper())
                value = value.strip().lower() or None
                if value is not None and value not in {"mock", "openai", "google"}:
                    raise ValueError(name.upper())
                object.__setattr__(self, name, value)

    @property
    def effective_classification_provider(self) -> str:
        return self.classification_provider or self.ai_provider.lower()

    @property
    def effective_embedding_provider(self) -> str:
        return self.embedding_provider or self.ai_provider.lower()

    @property
    def effective_remix_provider(self) -> str:
        return self.remix_provider or self.ai_provider.lower()

    @property
    def data_dir(self) -> Path:
        return self.reserve_path / "data"

    @property
    def run_logs_dir(self) -> Path:
        return self.data_dir / "run-logs"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def telegram_session_path(self) -> Path:
        return self.data_dir / "telegram.session"

    @property
    def media_dir(self) -> Path:
        return self.reserve_path / "media"

    @property
    def originals_dir(self) -> Path:
        return self.media_dir / "originals"

    @property
    def thumbnails_dir(self) -> Path:
        return self.media_dir / "thumbnails"

    @property
    def previews_dir(self) -> Path:
        return self.media_dir / "previews"

    @property
    def video_previews_dir(self) -> Path:
        return self.media_dir / "video-previews"

    @property
    def video_playback_dir(self) -> Path:
        return self.media_dir / "video-playback"

    @property
    def keyframes_dir(self) -> Path:
        return self.media_dir / "keyframes"

    @property
    def catalog_dir(self) -> Path:
        return self.reserve_path / "catalog"

    @property
    def remix_drafts_dir(self) -> Path:
        return self.data_dir / "remix-drafts"

    @property
    def dashboard_dist_dir(self) -> Path:
        return self.reserve_path / "dashboard" / "dist"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith(("postgresql", "postgres"))


ENV_MAP = {
    "owner_session_id": "OWNER_SESSION_ID",
    "app_env": "APP_ENV",
    "reserve_path": "RESERVE_PATH",
    "database_url": "DATABASE_URL",
    "telegram_api_id": "TELEGRAM_API_ID",
    "telegram_api_hash": "TELEGRAM_API_HASH",
    "google_api_key": "GEMINI_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "postgres_password": "POSTGRES_PASSWORD",
    "ai_provider": "AI_PROVIDER",
    "classification_provider": "CLASSIFICATION_PROVIDER",
    "embedding_provider": "EMBEDDING_PROVIDER",
    "remix_provider": "REMIX_PROVIDER",
    "ai_model": "AI_MODEL",
    "embedding_model": "EMBEDDING_MODEL",
    "embedding_dimensions": "EMBEDDING_DIMENSIONS",
    "online_embeddings_enabled": "ONLINE_EMBEDDINGS_ENABLED",
    "classification_prompt_version": "CLASSIFICATION_PROMPT_VERSION",
    "classification_style_guidance": "CLASSIFICATION_STYLE_GUIDANCE",
    "remix_style_guidance": "REMIX_STYLE_GUIDANCE",
    "ai_request_delay_seconds": "AI_REQUEST_DELAY_SECONDS",
    "ai_max_retries": "AI_MAX_RETRIES",
    "ai_max_retry_delay_seconds": "AI_MAX_RETRY_DELAY_SECONDS",
    "ai_stop_on_rate_limit": "AI_STOP_ON_RATE_LIMIT",
    "ai_credit_probe_interval_seconds": "AI_CREDIT_PROBE_INTERVAL_SECONDS",
    "ai_credit_probe_lease_seconds": "AI_CREDIT_PROBE_LEASE_SECONDS",
    "ai_recovery_batch_size": "AI_RECOVERY_BATCH_SIZE",
    "ai_include_video_original": "AI_INCLUDE_VIDEO_ORIGINAL",
    "ai_video_inline_max_mb": "AI_VIDEO_INLINE_MAX_MB",
    "import_batch_size": "IMPORT_BATCH_SIZE",
    "import_delay_seconds": "IMPORT_DELAY_SECONDS",
    "max_video_keyframes": "MAX_VIDEO_KEYFRAMES",
    "semantic_search_top_k": "SEMANTIC_SEARCH_TOP_K",
    "remix_model": "REMIX_MODEL",
    "remix_aspect_ratio": "REMIX_ASPECT_RATIO",
    "remix_image_size": "REMIX_IMAGE_SIZE",
    "remix_include_video_original": "REMIX_INCLUDE_VIDEO_ORIGINAL",
    "remix_video_inline_max_mb": "REMIX_VIDEO_INLINE_MAX_MB",
    "redis_url": "REDIS_URL",
    "public_site_url": "PUBLIC_SITE_URL",
    "public_share_url": "PUBLIC_SHARE_URL",
    "public_sharing_enabled": "PUBLIC_SHARING_ENABLED",
    "public_session_secret": "PUBLIC_SESSION_SECRET",
    "public_session_cookie_name": "PUBLIC_SESSION_COOKIE_NAME",
    "public_session_cookie_secure": "PUBLIC_SESSION_COOKIE_SECURE",
    "public_session_days": "PUBLIC_SESSION_DAYS",
    "public_trust_proxy_headers": "PUBLIC_TRUST_PROXY_HEADERS",
    "public_items_default_limit": "PUBLIC_ITEMS_DEFAULT_LIMIT",
    "public_items_max_limit": "PUBLIC_ITEMS_MAX_LIMIT",
    "public_remix_jobs_per_hour": "PUBLIC_REMIX_JOBS_PER_HOUR",
    "public_remix_analyses_per_hour": "PUBLIC_REMIX_ANALYSES_PER_HOUR",
    "public_write_actions_per_minute": "PUBLIC_WRITE_ACTIONS_PER_MINUTE",
    "public_max_active_remix_jobs": "PUBLIC_MAX_ACTIVE_REMIX_JOBS",
    "public_upload_max_mb": "PUBLIC_UPLOAD_MAX_MB",
    "browser_playback_backfill_batch_size": "BROWSER_PLAYBACK_BACKFILL_BATCH_SIZE",
    "remix_job_stale_seconds": "REMIX_JOB_STALE_SECONDS",
    "log_json": "ARSENAL_LOG_JSON",
}

FILE_ENV_MAP = {key: f"{env_name}_FILE" for key, env_name in ENV_MAP.items()}
GOOGLE_API_KEY_ALIASES = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "AI_API_KEY")


def _read_secret_file(value: str | None) -> str | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip()


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return loaded


def _settings_from_env(values: Mapping[str, str | None]) -> dict[str, str]:
    settings: dict[str, str] = {}
    for key, env_name in ENV_MAP.items():
        if key in ("classification_style_guidance", "remix_style_guidance"):
            # Bare dotenv keys are unspecified; explicit empty strings clear.
            value = values.get(env_name)
            if value is not None:
                settings[key] = value
            else:
                file_path = values.get(FILE_ENV_MAP[key])
                if file_path and Path(file_path).expanduser().exists():
                    # Keep controls until Settings validates, before outer trimming.
                    settings[key] = Path(file_path).expanduser().read_text(encoding="utf-8")
            continue
        file_value = _read_secret_file(values.get(FILE_ENV_MAP[key]))
        if key == "database_url" and file_value:
            settings[key] = file_value
            continue
        value = values.get(env_name)
        if value:
            settings[key] = value
            continue
        if file_value:
            settings[key] = file_value
            continue
    for env_name in GOOGLE_API_KEY_ALIASES:
        value = values.get(env_name)
        if value:
            settings["google_api_key"] = value
            break
        file_value = _read_secret_file(values.get(f"{env_name}_FILE"))
        if file_value:
            settings["google_api_key"] = file_value
            break
    return settings


def validate_owner_id(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 64
            or value != value.strip() or any(c in value for c in "\x00\r\n")):
        raise ValueError("Invalid OWNER_SESSION_ID representation")
    return value


def load_settings(config_path: str | Path | None = None, *, ignore_owner: bool = False) -> Settings:
    """Load settings with precedence: defaults < config.yaml < .env < environment."""

    candidates = [Path("config.yaml"), Path("config.yml")]
    if config_path:
        candidates.insert(0, Path(config_path))

    raw: dict[str, Any] = {}
    for candidate in candidates:
        if candidate.exists():
            raw.update(_read_yaml(candidate))
            break

    owner_values = []
    if raw.get("owner_session_id") is not None and not ignore_owner:
        owner_values.append(validate_owner_id(raw["owner_session_id"]))
    sources = []
    for dotenv_path in (Path("env"), Path(".env")):
        if dotenv_path.exists():
            sources.append(dotenv_values(dotenv_path))
    sources.append(os.environ)
    for source in sources:
        values = dict(source)
        inline = values.pop("OWNER_SESSION_ID", None)
        file = values.pop("OWNER_SESSION_ID_FILE", None)
        if not ignore_owner:
            if inline is not None:
                owner_values.append(validate_owner_id(inline))
            if file is not None:
                try:
                    with Path(file).open(encoding="utf-8", newline="") as handle:
                        value = handle.read().removesuffix("\n")
                    owner_values.append(validate_owner_id(value))
                except (OSError, UnicodeError, ValueError):
                    raise ValueError("OWNER_SESSION_ID_FILE must contain a readable valid owner ID") from None
        raw.update(_settings_from_env(values))
    raw.pop("owner_session_id", None)
    if owner_values:
        if len(set(owner_values)) != 1:
            raise ValueError("Conflicting OWNER_SESSION_ID settings")
        raw["owner_session_id"] = owner_values[0]

    int_keys = {
        "embedding_dimensions",
        "ai_max_retries",
        "ai_recovery_batch_size",
        "import_batch_size",
        "max_video_keyframes",
        "semantic_search_top_k",
        "public_session_days",
        "public_items_default_limit",
        "public_items_max_limit",
        "public_remix_jobs_per_hour",
        "public_remix_analyses_per_hour",
        "public_write_actions_per_minute",
        "public_max_active_remix_jobs",
        "browser_playback_backfill_batch_size",
        "remix_job_stale_seconds",
    }
    for key in int_keys:
        if key in raw and raw[key] is not None:
            raw[key] = int(raw[key])

    float_keys = {
        "ai_request_delay_seconds",
        "ai_max_retry_delay_seconds",
        "ai_credit_probe_interval_seconds",
        "ai_credit_probe_lease_seconds",
        "ai_video_inline_max_mb",
        "remix_video_inline_max_mb",
        "public_upload_max_mb",
        "import_delay_seconds",
    }
    for key in float_keys:
        if key in raw and raw[key] is not None:
            raw[key] = float(raw[key])

    for key in (
        "ai_stop_on_rate_limit",
        "online_embeddings_enabled",
        "ai_include_video_original",
        "remix_include_video_original",
        "public_session_cookie_secure",
        "public_sharing_enabled",
        "public_trust_proxy_headers",
        "log_json",
    ):
        if key in raw:
            if key == "public_sharing_enabled" and str(raw[key]).strip().lower() not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                raise ValueError("PUBLIC_SHARING_ENABLED must be a boolean")
            raw[key] = _coerce_bool(raw[key])
    if "reserve_path" in raw:
        raw["reserve_path"] = Path(raw["reserve_path"]).expanduser().resolve()
    if "database_url" not in raw:
        reserve_path = raw.get("reserve_path", DEFAULT_RESERVE_PATH)
        raw["database_url"] = f"sqlite:///{Path(reserve_path) / 'data' / 'arsenal.sqlite'}"

    return Settings(**raw)


RESERVE_DIRS = (
    "data",
    "data/run-logs",
    "data/exports",
    "media/originals",
    "media/thumbnails",
    "media/previews",
    "media/video-previews",
    "media/video-playback",
    "media/keyframes",
    "catalog",
    "catalog/search-results",
    "data/remix-drafts",
    "dashboard/dist",
)


def ensure_reserve_layout(settings: Settings) -> None:
    for relative in RESERVE_DIRS:
        (settings.reserve_path / relative).mkdir(parents=True, exist_ok=True)
