from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from meme_arsenal import online
from meme_arsenal.models import MediaItem, TelegramMessage


@pytest.mark.parametrize("processing_status", ["pending", "failed", "processed"])
@pytest.mark.parametrize("file_exists", [False, True])
def test_foreign_upload_is_hidden_before_filesystem_access(
    settings, monkeypatch, processing_status, file_exists,
):
    item = MediaItem(
        id=1, telegram_message_id=2, origin="dashboard_upload",
        visibility="private", public_status="draft", processing_status=processing_status,
        original_path=str((settings.originals_dir / "synthetic.png").relative_to(settings.reserve_path)),
    )
    if file_exists:
        (settings.originals_dir / "synthetic.png").write_bytes(b"synthetic")
    db = Mock()
    db.get.side_effect = [item, TelegramMessage(chat_id=online._upload_chat_id("other-session"))]
    file_check = Mock(side_effect=AssertionError("Unauthorized filesystem access"))
    monkeypatch.setattr(online, "media_item_has_original_file", file_check)

    with pytest.raises(HTTPException) as error:
        online._remix_media_item_or_404(db, settings, item.id, session_id="caller-session")

    assert (error.value.status_code, error.value.detail) == (404, "Item not found")
    file_check.assert_not_called()


@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("processing_status,file_exists,expected_status", [
    ("pending", True, 400),
    ("processed", False, 404),
    ("processed", True, None),
])
def test_eligible_sources_preserve_readiness_and_file_checks(
    settings, public, processing_status, file_exists, expected_status,
):
    item = MediaItem(
        id=1, telegram_message_id=2,
        origin="telegram" if public else "dashboard_upload",
        visibility="public" if public else "private",
        public_status="published" if public else "draft",
        processing_status=processing_status,
        original_path=str((settings.originals_dir / "synthetic.png").relative_to(settings.reserve_path)),
    )
    if file_exists:
        (settings.originals_dir / "synthetic.png").write_bytes(b"synthetic")
    db = Mock()
    db.get.side_effect = [item, TelegramMessage(chat_id=online._upload_chat_id("caller-session"))]

    if expected_status:
        with pytest.raises(HTTPException) as error:
            online._remix_media_item_or_404(db, settings, item.id, session_id="caller-session")
        assert error.value.status_code == expected_status
    else:
        assert online._remix_media_item_or_404(
            db, settings, item.id, session_id="caller-session",
        ) is item
    if public:
        db.get.assert_called_once_with(MediaItem, item.id)
