from __future__ import annotations

from copy import deepcopy
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

from rich.console import Console

from .config import Settings


console = Console()
SHARE_TOKEN_PATH = re.compile(r"(?P<prefix>/share/)[^/?\s]+")
SHORT_SHARE_PATH = re.compile(r"^/[A-Z2-9]{5}(?=[/?]|$)")


class RedactShareTokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            arguments = list(record.args)
            arguments[2] = SHARE_TOKEN_PATH.sub(r"\g<prefix>[redacted]", str(arguments[2]))
            arguments[2] = SHORT_SHARE_PATH.sub("/[redacted]", arguments[2])
            record.args = tuple(arguments)
        return True


def uvicorn_log_config() -> dict[str, Any]:
    from uvicorn.config import LOGGING_CONFIG

    config = deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_share_tokens"] = {
        "()": "meme_arsenal.logging.RedactShareTokenFilter",
    }
    access_handler = config.get("handlers", {}).get("access")
    if isinstance(access_handler, dict):
        access_handler["filters"] = [
            *access_handler.get("filters", []),
            "redact_share_tokens",
        ]
    return config


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_run_report(settings: Settings, name: str, payload: dict[str, Any]) -> Path:
    settings.run_logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = settings.run_logs_dir / f"{stamp}-{name}.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump({"created_at": utc_now_iso(), **payload}, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path
