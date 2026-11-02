from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

from meme_arsenal.db import session_scope
from meme_arsenal.maintenance import cleanup_online_state
from meme_arsenal.models import MediaItem, RemixJobRecord, TelegramMessage


def test_online_cleanup_is_dry_run_by_default_and_preserves_ready_jobs(settings):
    old = datetime.now(timezone.utc) - timedelta(days=120)
    with session_scope(settings) as session:
        message = TelegramMessage(telegram_message_id=1, chat_id="cleanup", date=old)
        session.add(message)
        session.flush()
        left = MediaItem(telegram_message_id=message.id, media_type="image", processing_status="processed")
        right = MediaItem(telegram_message_id=message.id, media_type="image", processing_status="processed")
        session.add_all([left, right])
        session.flush()
        session.add_all(
            [
                RemixJobRecord(
                    id="old-failed",
                    left_id=left.id,
                    right_id=right.id,
                    status="failed",
                    message="Failed",
                    updated_at=old,
                ),
                RemixJobRecord(
                    id="old-ready",
                    left_id=left.id,
                    right_id=right.id,
                    status="ready",
                    message="Ready",
                    updated_at=old,
                ),
            ]
        )

    failed_draft = settings.remix_drafts_dir / "old-failed.png"
    ready_draft = settings.remix_drafts_dir / "old-ready.png"
    orphan_draft = settings.remix_drafts_dir / "orphan.png"
    for path in (failed_draft, ready_draft, orphan_draft):
        path.write_bytes(b"draft")
        os.utime(path, (old.timestamp(), old.timestamp()))

    preview = cleanup_online_state(settings)
    assert preview["terminal_jobs"] == 1
    assert preview["orphan_drafts"] == 2
    assert failed_draft.exists()
    assert orphan_draft.exists()

    applied = cleanup_online_state(settings, apply=True)
    assert applied["jobs_removed"] == 1
    assert applied["drafts_removed"] == 2
    assert ready_draft.exists()
    with session_scope(settings) as session:
        assert session.get(RemixJobRecord, "old-failed") is None
        assert session.get(RemixJobRecord, "old-ready") is not None
