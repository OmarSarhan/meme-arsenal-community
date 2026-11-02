from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import time
import logging

import pytest
from sqlalchemy import func, select
import uvicorn

from meme_arsenal.config import Settings, ensure_reserve_layout
from meme_arsenal.dashboard import build_dashboard, write_online_dashboard_config
from meme_arsenal.db import session_scope
from meme_arsenal.models import AnonymousSession, MediaItem, RemixJobRecord, TelegramMessage
from meme_arsenal.online import create_app, refresh_public_indexes
from test_dashboard_chromium import _free_port, _make_image, _make_hover_video, _wait_for_json, chromium_page

pytestmark = pytest.mark.chromium


@dataclass
class LiveDashboard:
    url: str
    settings: Settings

    def add_image(self):
        with session_scope(self.settings) as db:
            message = TelegramMessage(telegram_message_id=time.time_ns(), chat_id="live-test", caption="New arrival")
            db.add(message)
            db.flush()
            item = MediaItem(telegram_message_id=message.id, media_type="image", processing_status="processed",
                             original_path="media/previews/card.jpg", preview_path="media/previews/card.jpg",
                             thumbnail_path="media/previews/card.jpg")
            db.add(item)
            db.flush()
            refresh_public_indexes(db, [item.id], settings=self.settings)
            return item.id


@pytest.fixture(scope="module")
def live_dashboard(tmp_path_factory):
    reserve = tmp_path_factory.mktemp("live-chromium")
    settings = Settings(owner_session_id="browser-owner", reserve_path=reserve, database_url=f"sqlite:///{reserve / 'test.sqlite'}",
                        ai_provider="google", google_api_key="test-only-no-provider-calls",
                        public_session_cookie_secure=False, public_session_secret="live-test-session-secret",
                        redis_url="redis://localhost:1/0")
    ensure_reserve_layout(settings)
    _make_image(settings.previews_dir / "card.jpg", "LIVE GALLERY", (40, 110, 130))
    _make_hover_video(settings.originals_dir / "video.mp4", with_audio=True)
    _make_hover_video(settings.video_previews_dir / "hover.mp4")
    with session_scope(settings) as db:
        db.add(AnonymousSession(id=settings.owner_session_id))
        for index in range(1, 241):
            message = TelegramMessage(telegram_message_id=index, chat_id="live-test", caption=f"Live item {index}")
            db.add(message)
            db.flush()
            video = index == 240
            db.add(MediaItem(telegram_message_id=message.id, media_type="video" if video else "image",
                             processing_status="processed", preview_path="media/previews/card.jpg",
                             thumbnail_path="media/previews/card.jpg",
                             original_path="media/originals/video.mp4" if video else "media/previews/card.jpg",
                             hover_preview_path="media/video-previews/hover.mp4" if video else None))
        db.flush()
        refresh_public_indexes(db, settings=settings)
    build_dashboard(settings)
    write_online_dashboard_config(settings)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/"
    try:
        _wait_for_json(url + "api/catalog-state")
        yield LiveDashboard(url, settings)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_live_refresh_preserves_scroll_and_open_detail(live_dashboard):
    with chromium_page(live_dashboard.url, width=390, height=840, mobile=True) as page:
        page.send("Page.addScriptToEvaluateOnNewDocument", {"source": """
          const interval = window.setInterval.bind(window);
          window.setInterval = (fn, ms, ...args) => interval(fn, ms === 20000 ? 200 : ms, ...args);
        """})
        page.send("Page.reload")
        page.wait_for_selector(".itemCard")
        for _ in range(6):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.evaluate("new Promise(resolve => setTimeout(resolve, 450))", await_promise=True)
        page.wait_for_selector('.itemCard[data-item-id="90"]')
        page.scroll_selector_to_center('.itemCard[data-item-id="90"]')
        before = page.evaluate("document.querySelector('.itemCard[data-item-id=\"90\"]').getBoundingClientRect().top")
        arrival = live_dashboard.add_image()
        page.wait_for_selector(f'.itemCard[data-item-id="{arrival}"]')
        page.evaluate("new Promise(resolve => setTimeout(resolve, 250))", await_promise=True)
        after = page.evaluate("document.querySelector('.itemCard[data-item-id=\"90\"]').getBoundingClientRect().top")
        assert abs(after - before) < 3
        page.evaluate("document.querySelector('.itemCard[data-item-id=\"90\"] .itemDetailButton').click()")
        page.wait_for_selector(".mobileBackButton")
        live_dashboard.add_image()
        page.evaluate("new Promise(resolve => setTimeout(resolve, 1200))", await_promise=True)
        assert "Live item 90" in page.evaluate("document.querySelector('.detail').textContent")
        page.evaluate("document.querySelector('.mobileBackButton').click()")
        page.wait_for_selector('.itemCard[data-item-id="90"]')
        page.evaluate(
            "new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))",
            await_promise=True,
        )
        page.evaluate("new Promise(resolve => setTimeout(resolve, 250))", await_promise=True)
        restored = page.evaluate("document.querySelector('.itemCard[data-item-id=\"90\"]').getBoundingClientRect().top")
        assert abs(restored - before) < 3


def test_live_video_playback_reports_native_events(live_dashboard, caplog, monkeypatch):
    monkeypatch.setattr(logging.getLogger("uvicorn"), "propagate", True)
    with caplog.at_level("INFO", logger="uvicorn.error"), chromium_page(live_dashboard.url, width=390, height=840, mobile=True) as page:
        selector = '.itemCard[data-item-id="240"] .playBadge'
        page.wait_for_selector(selector)
        page.scroll_selector_to_center(selector)
        page.evaluate(f"document.querySelector('{selector}').click()")
        page.evaluate("new Promise(resolve => setTimeout(resolve, 1800))", await_promise=True)
        records = [record.message for record in caplog.records if "playback_diagnostic" in record.message]
        assert any('"event": "play"' in record for record in records), records
        assert any('"event": "playing"' in record for record in records), records
        assert page.evaluate("document.querySelector('.itemCard[data-item-id=\"240\"] video').currentTime") > 0


def test_live_remix_restores_after_refresh_without_regeneration(live_dashboard):
    with chromium_page(live_dashboard.url, width=390, height=840, mobile=True) as page:
        page.wait_for_selector('.itemCard[data-item-id="239"]')
        page.evaluate("document.querySelector('.itemCard[data-item-id=\"239\"] [data-remix-side=\"left\"]').click()")
        page.evaluate("document.querySelector('.itemCard[data-item-id=\"238\"] [data-remix-side=\"right\"]').click()")
        page.wait_for_selector(".remixCreate:not(:disabled)")
        page.evaluate("document.querySelector('.remixCreate').click()")
        deadline = time.monotonic() + 10
        job_id = None
        while time.monotonic() < deadline:
            with session_scope(live_dashboard.settings) as db:
                job = db.scalar(select(RemixJobRecord).order_by(RemixJobRecord.created_at.desc()))
                if job:
                    job_id = job.id
                    _make_image(live_dashboard.settings.remix_drafts_dir / f"{job.id}.png", "RECOVERED REMIX", (70, 80, 90))
                    job.result_json = {"result": {"summary": "Recovered remix", "image_path": f"{job.id}.png"}}
                    job.status = "ready"
                    job.updated_at = datetime.now(timezone.utc)
            if job_id:
                break
            time.sleep(0.1)
        assert job_id
        page.wait_for_selector(".remixResultImageButton")
        page.send("Page.reload")
        page.wait_for_selector(".remixResultImageButton")
        assert "Live item 239" in page.evaluate("document.querySelectorAll('.remixSource')[0].textContent")
        assert "Live item 238" in page.evaluate("document.querySelectorAll('.remixSource')[1].textContent")
        assert "Recovered remix" in page.evaluate("document.querySelector('.remixPane').textContent")
        # Recover even when the create response or the local job pointer was lost.
        page.evaluate("sessionStorage.removeItem('meme-arsenal.remix.workspace.v1:/api/remix')")
        page.send("Page.reload")
        page.wait_for_selector(".remixResultImageButton")
        with session_scope(live_dashboard.settings) as db:
            assert db.scalar(select(func.count()).select_from(RemixJobRecord)) == 1
        # Retained selection must reach an older ready job behind >20 terminal
        # jobs, even after local dismissal. Discovery is independent of cookies.
        with session_scope(live_dashboard.settings) as db:
            for index in range(25):
                db.add(RemixJobRecord(id=f"terminal-{index:03}", session_id=live_dashboard.settings.owner_session_id,
                                     left_id=239, right_id=238, status="failed"))
        page.evaluate("sessionStorage.setItem('meme-arsenal.remix.workspace.v1:/api/remix', JSON.stringify({dismissed: ['" + job_id + "']}))")
        page.send("Page.reload")
        page.wait_for_selector('.remixPane')
        page.evaluate("Array.from(document.querySelectorAll('button')).find(b => b.textContent === 'Retained jobs').click()")
        page.wait_for_selector('select[aria-label="Retained jobs"]')
        page.evaluate("Array.from(document.querySelectorAll('button')).find(b => b.textContent === 'Load older').click()")
        page.wait_for_selector(f'select[aria-label="Retained jobs"] option[value="{job_id}"]')
        page.evaluate("const s = document.querySelector('select[aria-label=\"Retained jobs\"]'); s.value = '" + job_id + "'; s.dispatchEvent(new Event('change', {bubbles:true}))")
        page.wait_for_selector('.remixResultImageButton')
