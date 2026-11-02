"""Consent boundaries using only disposable fixture libraries."""
from dataclasses import replace
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from fastapi import HTTPException
import pytest
from sqlalchemy import select, func

from meme_arsenal.db import session_scope
from meme_arsenal.models import MediaItem, PublicMediaShare, PublicShareCode, RemixSourceRecord, TelegramMessage
from meme_arsenal.online import create_app, _locked_shareable_item
from meme_arsenal.telegram_importer import import_fixture_messages
from meme_arsenal.media import process_pending_media


def seed(settings, fixture_dir):
    with session_scope(settings) as db:
        import_fixture_messages(db, settings, fixture_dir)
        process_pending_media(db, settings)
        items = db.scalars(select(MediaItem).order_by(MediaItem.id)).all()
        items[0].origin = "remix"
        db.add(RemixSourceRecord(remix_media_item_id=items[0].id, source_media_item_id=items[1].id, position=1))
        return items[0].id, items[1].id


@pytest.mark.parametrize("short", [False, True])
def test_explicit_recipe_scope_and_recall(settings, fixture_dir, short):
    settings = replace(settings, public_sharing_enabled=True, public_share_url="https://share.example.test" if short else "")
    item_id, source_id = seed(settings, fixture_dir)
    client = TestClient(create_app(settings))
    endpoint = f"/api/items/{item_id}/share"
    output = client.post(endpoint).json()
    with session_scope(settings) as db:
        count = db.scalar(select(func.count()).select_from(PublicShareCode))
    status = client.get(endpoint + "?card=true").json()
    assert status["active"] is False and status["url"] is None
    assert client.get(f"/api/items/{item_id}/remix-card?page=true").status_code == 200
    with session_scope(settings) as db:
        assert db.scalar(select(func.count()).select_from(PublicShareCode)) == count
        assert db.scalar(select(PublicMediaShare)).recipe_grant_json is None
    assert client.get(urlsplit(output["url"]).path + "/card").status_code == 404
    recipe = client.post(endpoint + "?card=true").json()
    assert recipe["variant"] == "recipe" and recipe["active"]
    assert client.post(endpoint + "?card=true").json() == recipe
    card = urlsplit(recipe["card_url"]).path
    assert client.get(card).status_code == 200
    image = card + "/image" if not short else card + "/card/image"
    assert client.get(image + "?source=A").status_code == 404
    assert client.get(image + "?source=B").status_code == 200
    with session_scope(settings) as db:
        source = db.get(MediaItem, source_id)
        source.visibility = "private"
    assert client.get(image + "?source=B").status_code == 404
    assert client.delete(endpoint).status_code == 200
    assert client.get(card).status_code == 404
    assert client.post(endpoint + "?card=true").json()["url"] != recipe["url"]
    assert client.get(card).status_code == 404


def test_disabled_preserves_private_preview_and_recall(settings, fixture_dir):
    item_id, _ = seed(settings, fixture_dir)
    client = TestClient(create_app(settings))
    endpoint = f"/api/items/{item_id}/share"
    assert client.post(endpoint).status_code == 409
    assert client.get(endpoint).json()["enabled"] is False
    assert client.get(f"/api/items/{item_id}/remix-card?page=true").status_code == 200
    assert client.delete(endpoint).status_code == 200
    for path in ("/share/invalid", "/ABCDE/card/image?source=C"):
        response = client.get(path)
        assert response.status_code == 404 and "no-store" in response.headers["cache-control"]


def test_legacy_migration_never_revives_get_minted_cards(settings, fixture_dir):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from meme_arsenal.db import get_engine
    settings = replace(settings, public_sharing_enabled=True, public_share_url="https://share.example.test")
    item_id, _ = seed(settings, fixture_dir)
    app = create_app(settings)
    client = TestClient(app)
    endpoint = f"/api/items/{item_id}/share"
    client.post(endpoint)
    with session_scope(settings) as db:
        share = db.scalar(select(PublicMediaShare))
        old_signed = app.state.runtime.share_signer.dumps({"share": share.token_id, "card": True})
        db.add(PublicShareCode(code="ABCDE", token_id=share.token_id, card=True))
    engine = get_engine(settings)
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE public_media_shares DROP COLUMN recipe_grant_json")
        path = Path(__file__).resolve().parents[1] / "migrations/versions/013_explicit_recipe_grants.py"
        spec = importlib.util.spec_from_file_location("grant_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert connection.exec_driver_sql("SELECT recipe_grant_json IS NULL FROM public_media_shares").scalar()
    assert client.get("/ABCDE").status_code == 404
    new = client.post(endpoint + "?card=true").json()
    assert new["active"]
    assert client.get("/ABCDE").status_code == 404
    assert client.get(f"/share/{old_signed}/card").status_code == 404
    assert client.get(urlsplit(new["url"]).path).status_code == 200


def test_zero_slots_and_captured_original_permission(settings, fixture_dir):
    settings = replace(settings, public_sharing_enabled=True)
    item_id, source_id = seed(settings, fixture_dir)
    with session_scope(settings) as db:
        source = db.get(MediaItem, source_id)
        source.public_original_allowed = False
        source.preview_path = source.thumbnail_path = None
    client = TestClient(create_app(settings))
    endpoint = f"/api/items/{item_id}/share"
    recipe = client.post(endpoint + "?card=true").json()
    image = urlsplit(recipe["card_url"]).path + "/image?source=B"
    assert client.get(image).status_code == 404
    with session_scope(settings) as db:
        db.get(MediaItem, source_id).public_original_allowed = True
    assert client.get(image).status_code == 404
    client.delete(endpoint)
    with session_scope(settings) as db:
        db.get(MediaItem, source_id).visibility = "private"
        item = db.get(MediaItem, item_id)
        message = db.get(TelegramMessage, item.telegram_message_id)
        message.raw_metadata_json = {"remix_context": {"ingredients": [{"side": "B", "summary": "DENIED_SNAPSHOT_SENTINEL"}]}}
    zero = client.post(endpoint + "?card=true").json()
    card_path = urlsplit(zero["card_url"]).path
    page = client.get(card_path)
    assert zero["active"] and page.status_code == 200
    assert "DENIED_SNAPSHOT_SENTINEL" not in page.text
    assert client.get(card_path + "/image").status_code == 200
    denied = client.get(card_path + "/image?source=C")
    assert denied.status_code == 422 and "no-store" in denied.headers["cache-control"]
    with session_scope(settings) as db:
        assert db.scalar(select(PublicMediaShare)).recipe_grant_json == {"A": None, "B": None}


def test_retained_source_grant_availability_repair_and_independent_recall(settings, fixture_dir):
    from meme_arsenal.media import reserve_absolute, repair_missing_media_references
    settings = replace(settings, public_sharing_enabled=True)
    item_id, source_id = seed(settings, fixture_dir)
    client = TestClient(create_app(settings))
    endpoint = f"/api/items/{item_id}/share"
    client.post(f"/api/items/{source_id}/share")
    recipe = client.post(endpoint + "?card=true").json()
    image = urlsplit(recipe["card_url"]).path + "/image?source=B"
    assert client.delete(f"/api/items/{source_id}/share").status_code == 200
    assert client.get(image).status_code == 200
    with session_scope(settings) as db:
        source = db.get(MediaItem, source_id)
        original = reserve_absolute(settings, source.original_path)
    data = original.read_bytes()
    original.unlink()
    assert client.get(image).status_code == 404
    original.write_bytes(data)
    assert client.get(image).status_code == 200
    original.unlink()
    with session_scope(settings) as db:
        repair_missing_media_references(db, settings)
    original.write_bytes(data)
    assert client.get(image).status_code == 404


@pytest.mark.parametrize("origin", ["http://share.test", "https://private.example.test", "https://share.test:444", "https://user:secret@share.test", "https://bad..test", "https://share.test/path"])
def test_invalid_share_origins_fail_even_disabled(settings, origin):
    from meme_arsenal.online import _validate_online_settings
    with pytest.raises(RuntimeError):
        _validate_online_settings(replace(settings, public_site_url="https://private.example.test", public_share_url=origin))


@pytest.mark.parametrize("mutation", ["ineligible", "delete"])
def test_postgres_locked_eligibility_refresh(postgres_settings, fixture_dir, mutation):
    settings = postgres_settings
    item_id, _ = seed(settings, fixture_dir)
    # Transaction A has an old identity-map entity; B commits ineligibility before A locks.
    with session_scope(settings) as stale:
        item = stale.get(MediaItem, item_id)
        assert item.visibility == "public"
        with session_scope(settings) as writer:
            current = writer.get(MediaItem, item_id)
            if mutation == "delete":
                writer.delete(current)
            else:
                current.visibility = "private"
        with pytest.raises(HTTPException) as error:
            _locked_shareable_item(stale, settings, item_id)
        assert error.value.status_code == 404
        if mutation == "ineligible":
            assert item.visibility == "private"


def test_key_rotation_aliases_and_disabled_staging(settings, fixture_dir):
    settings = replace(settings, public_sharing_enabled=True, public_share_url="https://share.example.test")
    item_id, _ = seed(settings, fixture_dir)
    app = create_app(settings)
    client = TestClient(app)
    endpoint = f"/api/items/{item_id}/share"
    short = client.post(endpoint).json()
    path = urlsplit(short["url"]).path
    with session_scope(settings) as db:
        share = db.scalar(select(PublicMediaShare))
        token = app.state.runtime.share_signer.dumps({"share": share.token_id})
    rotated = TestClient(create_app(replace(settings, public_session_secret="synthetic-rotated-key")))
    assert rotated.get(path).status_code == 200
    assert rotated.get(f"/share/{token}").status_code == 404
    disabled = TestClient(create_app(replace(settings, public_sharing_enabled=False)))
    status = disabled.get(endpoint).json()
    assert status["active"] and not status["enabled"] and status["url"] is None
    assert disabled.get(path).status_code == 404
    assert client.get(path).status_code == 200  # Re-enabling retains the grant.
    assert disabled.delete(endpoint).status_code == 200
    assert client.get(path).status_code == 404
