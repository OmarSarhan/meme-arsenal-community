from __future__ import annotations

import json
import os
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from meme_arsenal.config import Settings, ensure_reserve_layout


@pytest.fixture()
def postgres_settings(settings):
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.schema import CreateSchema, DropSchema
    from meme_arsenal.db import dispose_session_engines

    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required for PostgreSQL concurrency tests")
    engine = create_engine(url)
    schema = f"test_{uuid.uuid4().hex}"
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.execute(CreateSchema(schema))
    isolated_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema},public"})
    try:
        isolated = replace(settings, database_url=isolated_url.render_as_string(hide_password=False))
        from meme_arsenal.db import session_scope
        from meme_arsenal.models import AnonymousSession
        with session_scope(isolated) as db:
            db.add(AnonymousSession(id=isolated.owner_session_id))
        yield isolated
    finally:
        dispose_session_engines()
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        engine.dispose()


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    reserve = tmp_path / "reserve"
    db_path = tmp_path / "arsenal.sqlite"
    loaded = Settings(
        owner_session_id="fixture-owner",
        reserve_path=reserve,
        database_url=f"sqlite:///{db_path}",
        ai_provider="mock",
        ai_model="mock-classifier",
        embedding_model="mock-embedding",
        embedding_dimensions=32,
        semantic_search_top_k=10,
    )
    ensure_reserve_layout(loaded)
    from meme_arsenal.db import session_scope
    from meme_arsenal.models import AnonymousSession
    with session_scope(loaded) as db:
        db.add(AnonymousSession(id=loaded.owner_session_id))
    return loaded


def make_image(path: Path, text: str, color: tuple[int, int, int]) -> None:
    image = Image.new("RGB", (640, 480), color=color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, 30, 610, 450), outline=(255, 255, 255), width=6)
    draw.text((56, 210), text, fill=(255, 255, 255))
    image.save(path)


@pytest.fixture()
def fixture_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "fixtures"
    directory.mkdir()
    make_image(directory / "deploy-fails.png", "deploy failed", (27, 94, 32))
    make_image(directory / "smug-reaction.png", "smug reaction", (94, 55, 25))
    manifest = {
        "messages": [
            {
                "telegram_message_id": 101,
                "caption": "when the deploy fails in production",
                "file": "deploy-fails.png",
                "date": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
                "telegram_post_url": "https://t.me/examplememes/101",
                "link_preview": {
                    "url": "https://example.com/deploy-fails",
                    "display_url": "example.com/deploy-fails",
                    "site_name": "Example Memes",
                    "title": "Deploy failed reaction",
                    "description": "A preview image used when production deploys fail.",
                    "has_preview_media": True,
                    "preview_media_kind": "photo",
                },
            },
            {
                "telegram_message_id": 102,
                "caption": "smug reaction image for a code review win",
                "file": "smug-reaction.png",
                "date": datetime(2026, 1, 2, tzinfo=timezone.utc).isoformat(),
            },
        ]
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory
