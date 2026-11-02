from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from meme_arsenal.catalog import export_catalog
from meme_arsenal.db import session_scope
from meme_arsenal.models import MediaItem, TelegramMessage
from meme_arsenal.online import create_app
from meme_arsenal.remix_server import _remix_payload


EXPECTED_ORDER = [5, 6, 10, 30, 20, 40, 70, 80]


@pytest.fixture()
def dated_archive(settings):
    settings = replace(settings, public_session_cookie_secure=False)
    client = TestClient(create_app(settings))
    Image.new("RGB", (8, 8)).save(settings.originals_dir / "sample.png")
    with session_scope(settings) as db:
        # Backfilled old Telegram media has larger IDs and later import times.
        # Uploads/remixes can also be saved long after their draft was created.
        for item_id, origin, saved_date, imported_date, published_date in [
            (10, "telegram", "2026-10-29", "2026-10-29", "2026-11-01"),
            (20, "telegram", "2026-10-28", "2026-10-30", None),
            (30, "telegram", "2026-10-28", "2026-10-31", None),
            (40, "telegram", "2026-05-08", "2026-11-01", "2026-11-01"),
            (50, "telegram", "2026-11-02", "2026-11-02", None),
            (5, "dashboard_upload", "2026-10-01", "2026-10-01", "2026-10-31"),
            (6, "remix", "2026-10-01", "2026-10-01", "2026-10-30"),
            (70, "telegram", None, "2026-04-01", None),
            (80, "dashboard_upload", "2026-03-01", "2026-11-01", None),
        ]:
            message = TelegramMessage(
                id=item_id, telegram_message_id=item_id, chat_id="fixture",
                date=datetime.fromisoformat(saved_date) if saved_date else None,
                caption="ordered sample",
            )
            db.add(message)
            db.flush()
            db.add(MediaItem(
                id=item_id, telegram_message_id=message.id, media_type="image",
                original_path="media/originals/sample.png" if item_id != 50 else "media/originals/missing.png",
                processing_status="processed",
                origin=origin,
                created_at=datetime.fromisoformat(imported_date),
                published_at=datetime.fromisoformat(published_date) if published_date else None,
            ))
    return settings, client


def test_online_saved_order_pagination_survives_deleted_cursor(dated_archive):
    settings, client = dated_archive
    first = client.get("/api/items", params={"limit": 2}).json()
    assert [item["id"] for item in first["items"]] == [5, 6]
    assert first["items"][0]["saved_at"] == "2026-10-31T00:00:00+00:00"
    assert first["items"][0]["added_at"] == "2026-10-01T00:00:00+00:00"
    cursor = first["next_cursor"]
    assert cursor == "saved:2026-10-30T00:00:00+00:00|6"

    # A deleted page boundary and a metadata edit must not change pagination.
    with session_scope(settings) as db:
        db.delete(db.get(MediaItem, 6))
        db.get(MediaItem, 10).updated_at = datetime(2027, 1, 1, tzinfo=timezone.utc)
    remaining = []
    while cursor is not None:
        response = client.get("/api/items", params={"limit": 2, "cursor": cursor})
        assert response.status_code == 200
        page = response.json()
        remaining.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
    assert remaining == EXPECTED_ORDER[2:]
    assert [item["id"] for item in client.get("/api/items").json()["items"]] == [
        item_id for item_id in EXPECTED_ORDER if item_id != 6
    ]
    assert client.get("/api/items", params={"cursor": "not-a-cursor"}).status_code == 400
    # Old cursors are translated using the item's save time, not import time.
    for legacy_cursor in [10, "2026-11-01T00:00:00|10"]:
        legacy = client.get("/api/items", params={"cursor": legacy_cursor}).json()
        assert [item["id"] for item in legacy["items"]] == EXPECTED_ORDER[3:]


def test_static_catalog_uses_same_saved_order(dated_archive):
    settings, client = dated_archive
    with session_scope(settings) as db:
        export_catalog(db, settings)
        local_saved_at = {
            item_id: _remix_payload(db, settings, db.get(MediaItem, item_id))["saved_at"]
            for item_id in EXPECTED_ORDER
        }
    payload = json.loads((settings.catalog_dir / "items-000.json").read_text())
    assert [item["id"] for item in payload["items"]] == EXPECTED_ORDER
    online_items = client.get("/api/items").json()["items"]
    assert [(item["id"], item["saved_at"]) for item in payload["items"]] == [
        (item["id"], item["saved_at"]) for item in online_items
    ]
    assert local_saved_at == {item["id"]: item["saved_at"] for item in online_items}


def test_online_search_uses_saved_order(dated_archive):
    _settings, client = dated_archive
    response = client.get("/api/items", params={"q": "ordered sample"})
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == EXPECTED_ORDER


def test_frontend_insert_refresh_and_updates_keep_saved_order(tmp_path):
    root = Path(__file__).resolve().parents[1]
    tsc = root / "frontend/dashboard/node_modules/.bin/tsc"
    node = shutil.which("node")
    if not node or not tsc.exists():
        pytest.skip("Node and installed dashboard dependencies are required")
    subprocess.run([
        str(tsc), str(root / "frontend/dashboard/src/catalogOrder.ts"),
        "--outDir", str(tmp_path), "--target", "es2020", "--module", "commonjs",
        "--skipLibCheck",
    ], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run([node, "-e", """
const assert = require('node:assert/strict');
const { mergeCatalogItems } = require('./catalogOrder.js');
const item = (id, saved_at) => ({ id, saved_at });
const newest = item(10, '2026-01-03T00:00:00Z');
const middle = item(30, '2026-01-02T00:00:00Z');
const older = item(20, '2026-01-02T00:00:00Z');
const oldest = item(40, '2026-01-01T00:00:00Z');
const ids = items => items.map(item => item.id);
let gallery = mergeCatalogItems([newest, middle], [oldest]); // Random selects an older, unloaded item.
assert.deepEqual(ids(gallery), [10, 30, 40]);
gallery = mergeCatalogItems(gallery, [middle, older]); // Pagination overlaps Random.
assert.deepEqual(ids(gallery), [10, 30, 20, 40]);
gallery = mergeCatalogItems(gallery, [{ ...oldest, added_at: '2027-01-01T00:00:00Z', summary: 'edited' }]);
assert.deepEqual(ids(gallery), [10, 30, 20, 40]);
assert.equal(gallery[3].summary, 'edited');
gallery = mergeCatalogItems(gallery, [newest, middle]); // Background refresh preserves order.
assert.deepEqual(ids(gallery), [10, 30, 20, 40]);
gallery = mergeCatalogItems(gallery, [item(5, '2026-01-04T00:00:00Z')]); // Save an older draft now.
assert.deepEqual(ids(gallery), [5, 10, 30, 20, 40]);
assert.deepEqual(ids(mergeCatalogItems([item(1)], [item(3), item(2)])), [3, 2, 1]);
assert.deepEqual(ids(mergeCatalogItems([], [
  { id: 3586, date: '2026-05-08T08:05:52Z', added_at: '2026-11-01T04:29:27Z' },
  { id: 3019, date: '2026-10-29T01:33:25Z', added_at: '2026-10-29T01:51:07Z' },
])), [3019, 3586]); // Older static catalogs still use the Telegram date.
assert.deepEqual(ids(mergeCatalogItems([], [
  item(1, '2026-01-03T01:00:00.123456+01:00'),
  item(2, '2026-01-03T00:00:00.123455Z'),
])), [1, 2]); // Preserve database precision and timezone equivalence.
"""], cwd=tmp_path, check=True, capture_output=True, text=True)
