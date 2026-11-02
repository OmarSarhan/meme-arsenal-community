from __future__ import annotations

import json
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PlaybackDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    attempt: str = Field(max_length=64)
    event: Literal["play", "playing", "stalled", "error", "fallback"]
    surface: Literal["gallery", "detail", "share"]
    source: Literal["playback", "preview", "original", "share"]
    elapsed_ms: int = Field(ge=0, le=86_400_000)
    current_time: float = Field(ge=0)
    ready_state: int = Field(ge=0, le=4)
    network_state: int = Field(ge=0, le=3)
    buffered_end: float = Field(ge=0)
    paused: bool
    muted: bool
    error_code: int | None = Field(default=None, ge=1, le=4)
    h264_aac: Literal["", "maybe", "probably"] = ""


def log_playback_diagnostic(item_id: int, user_agent: str, diagnostic: PlaybackDiagnostic) -> None:
    logging.getLogger("uvicorn.error").info(
        "playback_diagnostic %s",
        json.dumps({"item_id": item_id, "user_agent": user_agent[:400], **diagnostic.model_dump()}),
    )
