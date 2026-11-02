import asyncio
import io
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from meme_arsenal.db import session_scope
from meme_arsenal.models import MediaItem, TelegramMessage
from meme_arsenal.online import create_app
from meme_arsenal.remix_server import ArsenalDashboardHandler
from meme_arsenal.telegram_importer import import_saved_messages_async


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("External network disabled")
    monkeypatch.setattr("socket.socket.connect", blocked)


@pytest.mark.parametrize("legacy", [False, True])
def test_failed_download_retries_inside_scan_bounds(settings, monkeypatch, legacy):
    settings = replace(settings, telegram_api_id="123", telegram_api_hash="fake", import_delay_seconds=0)
    raw = SimpleNamespace(id=100, date=datetime.now(timezone.utc), message="meme", media=SimpleNamespace())
    attempts = []

    class Client:
        def __init__(self, *args, **kwargs): pass
        async def start(self): pass
        async def disconnect(self): pass
        async def get_messages(self, chat, ids):
            assert ids == [100]
            return [raw]
        async def iter_messages(self, chat, **kwargs):
            if kwargs.get("min_id", 0) < 100 < kwargs.get("max_id", 999):
                yield raw
        async def download_media(self, message, file):
            attempts.append(100)
            if len(attempts) == 1:
                raise OSError("temporary download failure")
            path = str(file) + "/meme.png"
            Image.new("RGB", (8, 8)).save(path)
            return path

    monkeypatch.setattr("telethon.TelegramClient", Client)
    for expected_failures in [1, 0, 0]:
        with session_scope(settings) as db:
            report = asyncio.run(import_saved_messages_async(db, settings, 10, backfill_older=False))
            assert report["failed"] == expected_failures
            if legacy and expected_failures:
                db.scalar(select(TelegramMessage)).import_status = "imported"
    assert attempts == [100, 100]
    with session_scope(settings) as db:
        assert db.scalar(select(TelegramMessage)).import_status == "imported"
        assert db.scalar(select(MediaItem)) is not None


@pytest.mark.parametrize("path", ["/api/remix/jobs", "/api/remix/uploads"])
def test_foreign_origin_rejected_before_mutation(path):
    handler = object.__new__(ArsenalDashboardHandler)
    handler.path = path
    handler.headers = {"Origin": "https://attacker.example", "Host": "localhost:8123"}
    handler._send_json = Mock()
    handler._handle_api_post = Mock()
    handler.do_POST()
    assert handler._send_json.call_args.args[1] == 403
    handler._handle_api_post.assert_not_called()


def test_json_content_type_and_same_origin():
    handler = object.__new__(ArsenalDashboardHandler)
    handler.path = "/api/remix/jobs"
    handler.headers = {"Origin": "http://localhost:8123", "Host": "localhost:8123", "Content-Type": "text/plain"}
    handler._send_json = Mock()
    handler._handle_api_post = Mock()
    handler.do_POST()
    assert handler._send_json.call_args.args[1] == 415
    handler._handle_api_post.assert_not_called()
    handler.headers["Content-Type"] = "application/json; charset=utf-8"
    handler.do_POST()
    handler._handle_api_post.assert_called_once_with(handler.path)


def test_draft_share_denied_and_existing_share_hidden(settings):
    settings = replace(settings, public_session_secret="test-only", public_session_cookie_secure=False,
                       public_share_url="https://share.example.test", public_sharing_enabled=True)
    app = create_app(settings)
    owner, other = TestClient(app), TestClient(app)
    data = io.BytesIO()
    Image.new("RGB", (8, 8)).save(data, format="PNG")
    response = owner.post("/api/remix/uploads", files={"file": ("draft.png", data.getvalue(), "image/png")})
    assert response.status_code == 200
    item_id = response.json()["id"]
    for client in [owner, other]:
        assert client.post(f"/api/items/{item_id}/share").status_code == 404
    assert owner.post(f"/api/remix/uploads/{item_id}/save").status_code == 200
    response = other.post(f"/api/items/{item_id}/share")
    assert response.status_code == 200
    url = response.json()["url"]
    public = TestClient(app, base_url="https://share.example.test")
    assert public.get(url + "/media").status_code == 200
    with session_scope(settings) as db:
        db.get(MediaItem, item_id).visibility = "private"
    assert public.get(url).status_code == 404
    assert public.get(url + "/media").status_code == 404
