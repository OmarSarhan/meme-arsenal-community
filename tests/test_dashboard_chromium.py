from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from typing import Any, Iterator
from urllib.parse import urlparse
from urllib.request import urlopen

from PIL import Image, ImageDraw
import pytest

from meme_arsenal.config import Settings, ensure_reserve_layout
from meme_arsenal.dashboard import build_dashboard
from meme_arsenal.remix_server import ArsenalDashboardHandler, RemixJobStore


pytestmark = pytest.mark.chromium


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        pytest.skip(f"{name} is required for Chromium dashboard tests")
    return path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_from_url(url: str) -> Any:
    with urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_for_json(url: str, timeout: float = 10.0) -> Any:
    started = time.monotonic()
    last_error: Exception | None = None
    while time.monotonic() - started < timeout:
        try:
            return _json_from_url(url)
        except Exception as exc:  # noqa: BLE001 - polling startup endpoint.
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {url}: {last_error}")


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("WebSocket connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class CdpWebSocket:
    def __init__(self, websocket_url: str) -> None:
        parsed = urlparse(websocket_url)
        self._host = parsed.hostname or "127.0.0.1"
        self._port = parsed.port or 80
        self._path = parsed.path
        if parsed.query:
            self._path = f"{self._path}?{parsed.query}"
        self._sock = socket.create_connection((self._host, self._port), timeout=5)
        self._next_id = 1
        self._connect()

    def _connect(self) -> None:
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET {self._path} HTTP/1.1\r\n"
            f"Host: {self._host}:{self._port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            response += self._sock.recv(4096)
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise ConnectionError(response.decode("utf-8", errors="replace"))

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _send_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = bytearray([0x81])
        if len(data) < 126:
            header.append(0x80 | len(data))
        elif len(data) < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", len(data)))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", len(data)))
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self._sock.sendall(bytes(header) + mask + masked)

    def _recv_json(self) -> dict[str, Any]:
        while True:
            first, second = _recv_exact(self._sock, 2)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", _recv_exact(self._sock, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", _recv_exact(self._sock, 8))[0]
            mask = _recv_exact(self._sock, 4) if masked else b""
            payload = _recv_exact(self._sock, length)
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 1:
                return json.loads(payload.decode("utf-8"))
            if opcode == 8:
                raise ConnectionError("WebSocket closed")
            if opcode == 9:
                self._send_pong(payload)

    def _send_pong(self, payload: bytes) -> None:
        header = bytearray([0x8A])
        header.append(0x80 | len(payload))
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._sock.sendall(bytes(header) + mask + masked)

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        message_id = self._next_id
        self._next_id += 1
        self._send_json({"id": message_id, "method": method, "params": params or {}})
        while True:
            message = self._recv_json()
            if message.get("id") != message_id:
                continue
            if "error" in message:
                raise AssertionError(message["error"])
            return message.get("result") or {}


@dataclass
class BrowserPage:
    process: subprocess.Popen[bytes]
    cdp: CdpWebSocket

    def close(self) -> None:
        self.cdp.close()
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.cdp.send(method, params)

    def evaluate(self, expression: str, *, await_promise: bool = False) -> Any:
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
            },
        )
        if "exceptionDetails" in result:
            raise AssertionError(result["exceptionDetails"])
        return (result.get("result") or {}).get("value")

    def wait_for_selector(self, selector: str, timeout_ms: int = 15_000) -> None:
        started = time.monotonic()
        while True:
            remaining_ms = max(1, timeout_ms - int((time.monotonic() - started) * 1000))
            try:
                self.evaluate(
                    f"""new Promise((resolve, reject) => {{
                      const started = Date.now();
                      const tick = () => {{
                        if (document.querySelector({json.dumps(selector)})) return resolve(true);
                        if (Date.now() - started > {remaining_ms}) {{
                          return reject(new Error('Timed out waiting for {selector}'));
                        }}
                        setTimeout(tick, 100);
                      }};
                      tick();
                    }})""",
                    await_promise=True,
                )
                return
            except AssertionError as exc:
                navigation_errors = ("Cannot find default execution context", "Execution context was destroyed")
                if not any(message in str(exc) for message in navigation_errors) or remaining_ms <= 1:
                    raise
                time.sleep(0.1)

    def scroll_selector_to_center(self, selector: str) -> dict[str, float]:
        return self.evaluate(
            f"""(() => {{
              const element = document.querySelector({json.dumps(selector)});
              if (!element) throw new Error('Missing {selector}');
              element.scrollIntoView({{ block: 'center', inline: 'center' }});
              const rect = element.getBoundingClientRect();
              return {{
                x: rect.left + rect.width / 2,
                y: rect.top + rect.height / 2,
                width: rect.width,
                height: rect.height,
                top: rect.top,
                bottom: rect.bottom
              }};
            }})()"""
        )


@contextmanager
def chromium_page(
    url: str,
    *,
    width: int = 1280,
    height: int = 900,
    mobile: bool = False,
) -> Iterator[BrowserPage]:
    chromium = _tool("chromium")
    remote_port = _free_port()
    user_data_dir = Path(tempfile.mkdtemp(prefix="arsenal-chromium-"))
    process = subprocess.Popen(
        [
            chromium,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            f"--window-size={width},{height}",
            f"--remote-debugging-port={remote_port}",
            f"--user-data-dir={user_data_dir}",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    page: BrowserPage | None = None
    try:
        _wait_for_json(f"http://127.0.0.1:{remote_port}/json/version")
        targets = _wait_for_json(f"http://127.0.0.1:{remote_port}/json/list")
        target = next(entry for entry in targets if entry.get("type") == "page")
        cdp = CdpWebSocket(target["webSocketDebuggerUrl"])
        page = BrowserPage(process=process, cdp=cdp)
        page.send("Runtime.enable")
        page.send("Page.enable")
        page.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 1,
                "mobile": mobile,
            },
        )
        yield page
    finally:
        if page:
            page.close()
        else:
            process.terminate()
        shutil.rmtree(user_data_dir, ignore_errors=True)


class QuietDashboardHandler(ArsenalDashboardHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib API.
        return


@contextmanager
def served_dashboard(settings: Settings) -> Iterator[str]:
    jobs = RemixJobStore(settings)
    handler = partial(QuietDashboardHandler, settings=settings, jobs=jobs)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/dashboard/dist/"
    finally:
        server.shutdown()
        server.server_close()
        jobs.shutdown()
        thread.join(timeout=5)


def _make_image(path: Path, label: str, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (320, 240), color=color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((18, 18, 302, 222), outline=(255, 255, 255), width=4)
    draw.text((36, 104), label, fill=(255, 255, 255))
    image.save(path)


def _make_hover_video(path: Path, *, with_audio: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        _tool("ffmpeg"),
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=duration=3:size=320x240:rate=12",
    ]
    if with_audio:
        command.extend(["-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest"])
    else:
        command.append("-an")
    command.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p"])
    if with_audio:
        command.extend(["-c:a", "aac", "-b:a", "96k"])
    command.extend(["-movflags", "+faststart", str(path)])
    subprocess.run(command, check=True)


def _catalog_item(
    *,
    item_id: int,
    media_type: str,
    summary: str,
    preview_path: str,
    hover_preview_path: str | None = None,
    playback_path: str | None = None,
    original_path: str | None = None,
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "telegram_message_id": 10_000 + item_id,
        "chat_id": "test",
        "telegram_post_url": f"https://t.me/publicmemes/{10_000 + item_id}",
        "date": "2026-06-14T12:00:00+00:00",
        "caption": summary,
        "source_url": None,
        "source_display_url": None,
        "source_site": None,
        "source_title": None,
        "source_description": None,
        "source_embed_url": None,
        "link_preview": {},
        "media_type": media_type,
        "mime_type": "video/mp4" if media_type == "video" else "image/jpeg",
        "thumbnail_path": preview_path,
        "preview_path": preview_path,
        "hover_preview_path": hover_preview_path,
        "playback_path": playback_path,
        "original_path": original_path or preview_path,
        "duration_seconds": duration_seconds,
        "width": 320,
        "height": 240,
        "size_bytes": 1234,
        "duplicate_group_id": None,
        "processing_status": "processed",
        "classification_status": "ok",
        "classification_block_reason": None,
        "summary": summary,
        "visible_text": summary.upper(),
        "narration": "",
        "language": "English",
        "meme_format": "reaction meme",
        "tone": ["playful"],
        "reaction_type": ["approval"],
        "topics": ["dashboard"],
        "keywords": ["hover", "video"] if media_type == "video" else ["static", "image"],
        "people_or_characters": [],
        "safety_flags": [],
        "search_phrases": [summary],
        "favorite": False,
        "archived": False,
        "manual_tags": [],
        "notes": "",
        "remix_sources": [],
        "remix_used_by": [],
    }


@pytest.fixture(scope="module")
def chromium_dashboard(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    _tool("chromium")
    _tool("npm")
    _tool("ffmpeg")

    reserve = tmp_path_factory.mktemp("chromium-reserve")
    settings = Settings(
        reserve_path=reserve,
        database_url=f"sqlite:///{reserve / 'data' / 'arsenal.sqlite'}",
        ai_provider="mock",
    )
    ensure_reserve_layout(settings)

    _make_image(settings.previews_dir / "image-card.jpg", "STATIC IMAGE", (42, 96, 143))
    _make_image(settings.previews_dir / "video-poster.jpg", "HOVER VIDEO", (120, 42, 118))
    _make_hover_video(settings.video_previews_dir / "hover-card.mp4")
    _make_hover_video(settings.video_playback_dir / "playback-card.mp4", with_audio=True)
    _make_hover_video(settings.originals_dir / "original-card.mp4", with_audio=True)

    items = [
        _catalog_item(
            item_id=1,
            media_type="image",
            summary="static dashboard image",
            preview_path="../../media/previews/image-card.jpg",
        ),
        _catalog_item(
            item_id=2,
            media_type="video",
            summary="hover video preview target",
            preview_path="../../media/previews/video-poster.jpg",
            hover_preview_path="../../media/video-previews/hover-card.mp4",
            playback_path="../../media/video-playback/playback-card.mp4",
            original_path="../../media/originals/original-card.mp4",
            duration_seconds=3.0,
        ),
    ]
    (settings.catalog_dir / "items-000.json").write_text(json.dumps({"items": items}), encoding="utf-8")
    (settings.catalog_dir / "tags.json").write_text(
        json.dumps(
            {
                "media_type": [{"value": "image", "count": 1}, {"value": "video", "count": 1}],
                "topics": [{"value": "dashboard", "count": 2}],
                "tone": [{"value": "playful", "count": 2}],
                "reaction_type": [{"value": "approval", "count": 2}],
                "meme_format": [{"value": "reaction meme", "count": 2}],
                "language": [{"value": "English", "count": 2}],
            }
        ),
        encoding="utf-8",
    )
    (settings.catalog_dir / "index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "generated_at": "2026-06-14T12:00:00+00:00",
                "item_count": len(items),
                "chunk_size": 250,
                "chunks": ["../../catalog/items-000.json"],
                "tags_path": "../../catalog/tags.json",
                "search_index_path": "../../catalog/search-index.json",
                "search_results_base": "../../catalog/search-results",
            }
        ),
        encoding="utf-8",
    )
    (settings.catalog_dir / "search-results" / "semantic-cousin.json").write_text(
        json.dumps(
            {
                "query": "semantic cousin",
                "results": [
                    {
                        "media_item_id": 2,
                        "score": 0.92,
                        "semantic_score": 0.92,
                        "lexical_score": 0,
                        "explanation": "test bundle",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    build_dashboard(settings)
    with served_dashboard(settings) as url:
        yield url


@pytest.fixture()
def page(chromium_dashboard: str) -> Iterator[BrowserPage]:
    with chromium_page(chromium_dashboard) as browser_page:
        browser_page.wait_for_selector(".itemCard")
        yield browser_page


@pytest.fixture()
def mobile_page(chromium_dashboard: str) -> Iterator[BrowserPage]:
    with chromium_page(chromium_dashboard, width=390, height=840, mobile=True) as browser_page:
        browser_page.wait_for_selector(".itemCard")
        yield browser_page


def test_chromium_dashboard_loads_gallery(page: BrowserPage) -> None:
    state = page.evaluate(
        """(() => ({
          title: document.title,
          error: document.querySelector('.errorState')?.textContent || '',
          cardCount: document.querySelectorAll('.itemCard').length,
          videoCount: document.querySelectorAll('.mediaPreview.videoPreview').length,
          sectionText: document.querySelector('.sectionBar strong')?.textContent || ''
        }))()"""
    )
    assert state == {
        "title": "Meme Arsenal",
        "error": "",
        "cardCount": 2,
        "videoCount": 1,
        "sectionText": "2 of 2 shown",
    }


def test_chromium_contact_sheet_clips_accents(page: BrowserPage) -> None:
    clip_state = page.evaluate(
        """(() => {
          const grid = document.querySelector('.galleryGrid');
          const style = getComputedStyle(grid);
          return {
            overflowX: style.overflowX,
            overflowY: style.overflowY,
            contain: style.contain
          };
        })()"""
    )
    assert clip_state["overflowX"] == "hidden"
    assert clip_state["overflowY"] == "hidden"
    assert "paint" in clip_state["contain"]


def test_chromium_dark_mode_toggle_uses_theme_variables_and_persists(page: BrowserPage) -> None:
    initial = page.evaluate(
        """(() => {
          const button = document.querySelector('.themeToggleButton');
          return {
            label: button.getAttribute('aria-label'),
            pressed: button.getAttribute('aria-pressed'),
            hasDarkClass: document.querySelector('.appShell').classList.contains('isDarkMode'),
            storage: window.localStorage.getItem('meme-arsenal.darkMode')
          };
        })()"""
    )
    enabled = page.evaluate(
        """new Promise((resolve) => {
          document.querySelector('.themeToggleButton').click();
          requestAnimationFrame(() => {
            const shell = document.querySelector('.appShell');
            const button = document.querySelector('.themeToggleButton');
            const preview = document.querySelector('.previewImage');
            resolve({
              label: button.getAttribute('aria-label'),
              pressed: button.getAttribute('aria-pressed'),
              hasDarkClass: shell.classList.contains('isDarkMode'),
              shellFilter: getComputedStyle(shell).filter,
              shellColor: getComputedStyle(shell).color,
              previewFilter: getComputedStyle(preview).filter,
              storage: window.localStorage.getItem('meme-arsenal.darkMode')
            });
          });
        })""",
        await_promise=True,
    )
    page.evaluate("document.querySelector('.remixButton').click()")
    page.wait_for_selector(".schizoButton input[type=range]")
    dark_slider = page.evaluate(
        """(() => {
          const shell = document.querySelector('.appShell');
          const slider = document.querySelector('.schizoButton');
          const input = slider.querySelector('input[type="range"]');
          return {
            inDarkShell: Boolean(slider.closest('.appShell.isDarkMode')),
            shellFilter: getComputedStyle(shell).filter,
            inputFilter: getComputedStyle(input).filter,
            fill: slider.style.getPropertyValue('--slider-fill').trim(),
            thumb: slider.style.getPropertyValue('--slider-thumb').trim()
          };
        })()"""
    )
    disabled = page.evaluate(
        """new Promise((resolve) => {
          document.querySelector('.themeToggleButton').click();
          requestAnimationFrame(() => {
            const shell = document.querySelector('.appShell');
            const button = document.querySelector('.themeToggleButton');
            resolve({
              label: button.getAttribute('aria-label'),
              pressed: button.getAttribute('aria-pressed'),
              hasDarkClass: shell.classList.contains('isDarkMode'),
              shellFilter: getComputedStyle(shell).filter,
              storage: window.localStorage.getItem('meme-arsenal.darkMode')
            });
          });
        })""",
        await_promise=True,
    )

    assert initial == {
        "label": "Turn dark mode on",
        "pressed": "false",
        "hasDarkClass": False,
        "storage": "light",
    }
    assert enabled["label"] == "Turn dark mode off"
    assert enabled["pressed"] == "true"
    assert enabled["hasDarkClass"] is True
    assert enabled["shellFilter"] == "none"
    assert enabled["shellColor"] == "rgb(244, 244, 245)"
    assert enabled["previewFilter"] == "none"
    assert enabled["storage"] == "dark"
    assert dark_slider["inDarkShell"] is True
    assert dark_slider["shellFilter"] == "none"
    assert dark_slider["inputFilter"] == "none"
    assert dark_slider["fill"].startswith("hsl(")
    assert dark_slider["thumb"].startswith("hsl(")
    assert disabled == {
        "label": "Turn dark mode on",
        "pressed": "false",
        "hasDarkClass": False,
        "shellFilter": "none",
        "storage": "light",
    }


def test_chromium_filter_button_turns_green_only_for_active_filters(page: BrowserPage) -> None:
    initial = page.evaluate(
        """(() => {
          const button = document.querySelector('.filterButton');
          const style = getComputedStyle(button);
          return {
            pressed: button.getAttribute('aria-pressed'),
            active: button.classList.contains('isActive'),
            background: style.backgroundColor
          };
        })()"""
    )
    search_only = page.evaluate(
        """new Promise((resolve) => {
          const input = document.querySelector('.searchBox input');
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
          setter.call(input, 'hover video');
          input.dispatchEvent(new Event('input', { bubbles: true }));
          setTimeout(() => {
            const button = document.querySelector('.filterButton');
            resolve({
              pressed: button.getAttribute('aria-pressed'),
              active: button.classList.contains('isActive')
            });
          }, 350);
        })""",
        await_promise=True,
    )
    active_filter = page.evaluate(
        """new Promise((resolve) => {
          document.querySelector('.filterButton').click();
          const mediaSelect = document.querySelector('.filters .field select');
          const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
          setter.call(mediaSelect, 'video');
          mediaSelect.dispatchEvent(new Event('change', { bubbles: true }));
          requestAnimationFrame(() => {
            const button = document.querySelector('.filterButton');
            const style = getComputedStyle(button);
            resolve({
              pressed: button.getAttribute('aria-pressed'),
              active: button.classList.contains('isActive'),
              background: style.backgroundColor,
              color: style.color
            });
          });
        })""",
        await_promise=True,
    )
    reset = page.evaluate(
        """new Promise((resolve) => {
          document.querySelector('.filters .secondaryButton').click();
          requestAnimationFrame(() => {
            const button = document.querySelector('.filterButton');
            resolve({
              pressed: button.getAttribute('aria-pressed'),
              active: button.classList.contains('isActive')
            });
          });
        })""",
        await_promise=True,
    )

    assert initial["pressed"] == "false"
    assert initial["active"] is False
    assert search_only == {"pressed": "false", "active": False}
    assert active_filter == {
        "pressed": "true",
        "active": True,
        "background": "rgb(21, 128, 61)",
        "color": "rgb(255, 255, 255)",
    }
    assert reset == {"pressed": "false", "active": False}


def test_chromium_detail_panel_resizes_without_collapsing_gallery(page: BrowserPage) -> None:
    page.wait_for_selector(".detailResizeHandle")
    before = page.evaluate(
        """(() => ({
          detailWidth: document.querySelector('.detail').getBoundingClientRect().width,
          contentWidth: document.querySelector('.content').getBoundingClientRect().width,
          handleDisplay: getComputedStyle(document.querySelector('.detailResizeHandle')).display
        }))()"""
    )
    compact = page.evaluate(
        """new Promise((resolve) => {
          const handle = document.querySelector('.detailResizeHandle');
          handle.dispatchEvent(new KeyboardEvent('keydown', {
            bubbles: true,
            cancelable: true,
            key: 'Home'
          }));
          requestAnimationFrame(() => {
            resolve({
              detailWidth: document.querySelector('.detail').getBoundingClientRect().width,
              contentWidth: document.querySelector('.content').getBoundingClientRect().width,
              storedWidth: window.localStorage.getItem('meme-arsenal.detailPanelWidth')
            });
          });
        })""",
        await_promise=True,
    )
    after = page.evaluate(
        """new Promise((resolve) => {
          const handle = document.querySelector('.detailResizeHandle');
          handle.scrollIntoView({ block: 'center', inline: 'center' });
          const rect = handle.getBoundingClientRect();
          const x = rect.left + rect.width / 2;
          const y = rect.top + rect.height / 2;
          handle.dispatchEvent(new PointerEvent('pointerdown', {
            bubbles: true,
            cancelable: true,
            button: 0,
            clientX: x,
            clientY: y,
            pointerId: 1,
            pointerType: 'mouse'
          }));
          window.dispatchEvent(new PointerEvent('pointermove', {
            bubbles: true,
            cancelable: true,
            clientX: x - 96,
            clientY: y,
            pointerId: 1,
            pointerType: 'mouse'
          }));
          window.dispatchEvent(new PointerEvent('pointerup', {
            bubbles: true,
            cancelable: true,
            clientX: x - 96,
            clientY: y,
            pointerId: 1,
            pointerType: 'mouse'
          }));
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              resolve({
                detailWidth: document.querySelector('.detail').getBoundingClientRect().width,
                contentWidth: document.querySelector('.content').getBoundingClientRect().width,
                storedWidth: window.localStorage.getItem('meme-arsenal.detailPanelWidth')
              });
            });
          });
        })""",
        await_promise=True,
    )

    assert before["handleDisplay"] != "none"
    assert compact["detailWidth"] < before["detailWidth"]
    assert compact["storedWidth"] is not None
    assert after["detailWidth"] >= compact["detailWidth"] + 80
    assert after["contentWidth"] >= 420
    assert after["storedWidth"] is not None


def test_chromium_search_filters_gallery(page: BrowserPage) -> None:
    page.evaluate(
        """(() => {
          const input = document.querySelector('.searchBox input');
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
          setter.call(input, 'ho');
          input.dispatchEvent(new Event('input', { bubbles: true }));
        })()"""
    )
    page.evaluate("new Promise((resolve) => setTimeout(resolve, 300))", await_promise=True)
    short_state = page.evaluate(
        """(() => ({
          cardCount: document.querySelectorAll('.itemCard').length,
          sectionText: document.querySelector('.sectionBar strong')?.textContent || ''
        }))()"""
    )
    assert short_state["cardCount"] == 2
    assert short_state["sectionText"] == "2 of 2 shown"

    page.evaluate(
        """(() => {
          const input = document.querySelector('.searchBox input');
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
          setter.call(input, 'hover video');
          input.dispatchEvent(new Event('input', { bubbles: true }));
        })()"""
    )
    page.evaluate("new Promise((resolve) => setTimeout(resolve, 450))", await_promise=True)
    state = page.evaluate(
        """(() => ({
          cardCount: document.querySelectorAll('.itemCard').length,
          sectionText: document.querySelector('.sectionBar strong')?.textContent || '',
          summary: document.querySelector('.itemBody strong')?.textContent || ''
        }))()"""
    )
    assert state["cardCount"] == 1
    assert state["sectionText"] == "1 of 1 shown"
    assert "hover video preview target" in state["summary"].lower()


def test_chromium_static_search_bundle_enriches_gallery(page: BrowserPage) -> None:
    page.evaluate(
        """(() => {
          const input = document.querySelector('.searchBox input');
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
          setter.call(input, 'semantic cousin');
          input.dispatchEvent(new Event('input', { bubbles: true }));
        })()"""
    )
    page.evaluate("new Promise((resolve) => setTimeout(resolve, 450))", await_promise=True)
    state = page.evaluate(
        """(() => ({
          cardCount: document.querySelectorAll('.itemCard').length,
          sectionText: document.querySelector('.sectionBar strong')?.textContent || '',
          summary: document.querySelector('.itemBody strong')?.textContent || ''
        }))()"""
    )
    assert state["cardCount"] == 1
    assert state["sectionText"] == "1 of 1 shown"
    assert "hover video preview target" in state["summary"].lower()


def test_chromium_video_preview_uses_hover_clip_without_controls(page: BrowserPage) -> None:
    video_state = page.evaluate(
        """(() => {
          const video = document.querySelector('.mediaPreview.videoPreview video.hoverVideo');
          return {
            src: video.currentSrc || video.src,
            previewSrc: video.dataset.previewSrc,
            manualSrc: video.dataset.manualSrc,
            controls: video.controls,
            disablePictureInPicture: video.disablePictureInPicture,
            disableRemotePlayback: video.disableRemotePlayback,
            muted: video.muted,
            loop: video.loop,
            playsInline: video.playsInline,
            preload: video.preload
          };
        })()"""
    )
    assert video_state["src"] == ""
    assert video_state["previewSrc"].endswith("/media/video-previews/hover-card.mp4")
    assert video_state["manualSrc"].endswith("/media/video-playback/playback-card.mp4")
    assert video_state["controls"] is False
    assert video_state["disablePictureInPicture"] is True
    assert video_state["disableRemotePlayback"] is True
    assert video_state["muted"] is True
    assert video_state["loop"] is True
    assert video_state["playsInline"] is True
    assert video_state["preload"] == "none"
    play_button = page.evaluate(
        """(() => {
          const button = document.querySelector('.mediaPreview.videoPreview .playBadge');
          return {
            tagName: button?.tagName || '',
            label: button?.getAttribute('aria-label') || '',
            nestedInButton: Boolean(button?.parentElement?.closest('button')),
          };
        })()"""
    )
    assert play_button == {
        "tagName": "BUTTON",
        "label": "Play video with sound",
        "nestedInButton": False,
    }


def test_chromium_detail_video_uses_browser_playback_with_audio(page: BrowserPage) -> None:
    page.evaluate("document.querySelectorAll('.itemCard')[1].click()")
    page.wait_for_selector(".detailMedia video")
    state = page.evaluate(
        """(() => {
          const video = document.querySelector('.detailMedia video');
          const original = document.querySelector('.detailActions a.primaryButton');
          return {
            videoSrc: video.getAttribute('src') || '',
            preload: video.preload,
            disablePictureInPicture: video.disablePictureInPicture,
            disableRemotePlayback: video.disableRemotePlayback,
            poster: video.getAttribute('poster') || '',
            originalHref: original?.getAttribute('href') || ''
          };
        })()"""
    )
    assert state["videoSrc"].endswith("/media/video-playback/playback-card.mp4")
    assert state["preload"] == "metadata"
    assert state["disablePictureInPicture"] is True
    assert state["disableRemotePlayback"] is True
    assert state["poster"].endswith("/media/previews/video-poster.jpg")
    assert state["originalHref"].endswith("/media/originals/original-card.mp4")


def test_chromium_remix_source_thumbnail_overlays_stay_inside_preview(page: BrowserPage) -> None:
    page.evaluate("document.querySelectorAll('.itemCard')[1].click()")
    page.wait_for_selector(".detailMedia video")
    page.evaluate("document.querySelector('.detailRemixAction').click()")
    page.wait_for_selector(".remixSource .mediaPreview.videoPreview .typeBadge")
    overlay_state = page.evaluate(
        """(() => {
          const preview = document.querySelector('.remixSource .mediaPreview.videoPreview');
          const typeBadge = preview.querySelector('.typeBadge');
          const durationBadge = preview.querySelector('.durationBadge');
          const previewRect = preview.getBoundingClientRect();
          const typeRect = typeBadge.getBoundingClientRect();
          const durationRect = durationBadge.getBoundingClientRect();
          const typeStyle = getComputedStyle(typeBadge);
          const durationStyle = getComputedStyle(durationBadge);
          return {
            typeText: typeBadge.textContent,
            durationText: durationBadge.textContent,
            typeColor: typeStyle.color,
            durationColor: durationStyle.color,
            typeLeftInside: typeRect.left >= previewRect.left,
            typeBottomInside: typeRect.bottom <= previewRect.bottom,
            durationRightInside: durationRect.right <= previewRect.right,
            durationBottomInside: durationRect.bottom <= previewRect.bottom
          };
        })()"""
    )

    assert overlay_state["typeText"] == "video"
    assert overlay_state["durationText"] == "0:03"
    assert overlay_state["typeColor"] == "rgb(255, 255, 255)"
    assert overlay_state["durationColor"] == "rgb(255, 255, 255)"
    assert overlay_state["typeLeftInside"] is True
    assert overlay_state["typeBottomInside"] is True
    assert overlay_state["durationRightInside"] is True
    assert overlay_state["durationBottomInside"] is True


def test_chromium_detail_media_drags_to_remix_slots(page: BrowserPage) -> None:
    page.evaluate("document.querySelector('.remixButton').click()")
    page.wait_for_selector(".remixPane")

    image_state = page.evaluate(
        """new Promise((resolve) => {
          const media = document.querySelector('.detailMedia');
          const slot = document.querySelectorAll('.remixSlot')[0];
          const data = new DataTransfer();
          media.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer: data }));
          setTimeout(() => {
            const shimmerShown = document.querySelectorAll('.remixSlot.dropHint').length === 2;
            slot.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: data }));
            slot.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: data }));
            media.dispatchEvent(new DragEvent('dragend', { bubbles: true, cancelable: true, dataTransfer: data }));
            setTimeout(() => {
              const source = document.querySelectorAll('.remixSource')[0];
              resolve({
                shimmerShown,
                sourceText: source?.textContent || '',
                sourcePreviewTag: source?.querySelector('.mediaPreview img.previewImage')?.tagName || ''
              });
            }, 80);
          }, 80);
        })""",
        await_promise=True,
    )

    page.evaluate("document.querySelectorAll('.itemCard')[1].click()")
    page.wait_for_selector(".detailMedia video")
    video_state = page.evaluate(
        """new Promise((resolve) => {
          const media = document.querySelector('.detailMedia');
          const slot = document.querySelectorAll('.remixSlot')[1];
          const data = new DataTransfer();
          media.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer: data }));
          setTimeout(() => {
            const shimmerShown = document.querySelectorAll('.remixSlot.dropHint').length === 2;
            slot.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: data }));
            slot.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: data }));
            media.dispatchEvent(new DragEvent('dragend', { bubbles: true, cancelable: true, dataTransfer: data }));
            setTimeout(() => {
              const source = document.querySelectorAll('.remixSource')[1];
              resolve({
                shimmerShown,
                sourceText: source?.textContent || '',
                hasVideoPreview: Boolean(source?.querySelector('.mediaPreview.videoPreview')),
                typeBadge: source?.querySelector('.typeBadge')?.textContent || ''
              });
            }, 80);
          }, 80);
        })""",
        await_promise=True,
    )

    assert image_state["shimmerShown"] is True
    assert "static dashboard image" in image_state["sourceText"].lower()
    assert image_state["sourcePreviewTag"] == "IMG"
    assert video_state["shimmerShown"] is True
    assert "hover video preview target" in video_state["sourceText"].lower()
    assert video_state["hasVideoPreview"] is True
    assert video_state["typeBadge"] == "video"


def test_chromium_remix_panel_does_not_shift_when_sticky(page: BrowserPage) -> None:
    page.evaluate("document.querySelector('.remixButton').click()")
    page.wait_for_selector(".remixPane")
    mask_state = page.evaluate(
        """(() => {
          const pane = document.querySelector('.remixPane');
          const paneStyle = getComputedStyle(pane);
          const maskStyle = getComputedStyle(pane, '::before');
          return {
            top: paneStyle.top,
            maskHeight: maskStyle.height,
            maskTop: maskStyle.top,
            maskBackground: maskStyle.backgroundColor,
            maskContent: maskStyle.content,
          };
        })()"""
    )
    top_before = page.evaluate(
        """new Promise((resolve) => {
          document.body.style.minHeight = '2400px';
          document.querySelector('.content').style.minHeight = '2400px';
          document.documentElement.style.scrollBehavior = 'auto';
          window.scrollTo(0, 260);
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              resolve(document.querySelector('.remixPane').getBoundingClientRect().top);
            });
          });
        })""",
        await_promise=True,
    )
    top_after = page.evaluate(
        """new Promise((resolve) => {
          window.scrollTo(0, 420);
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              resolve(document.querySelector('.remixPane').getBoundingClientRect().top);
            });
          });
        })""",
        await_promise=True,
    )

    assert mask_state == {
        "top": "73px",
        "maskHeight": "73px",
        "maskTop": "-73px",
        "maskBackground": "rgb(251, 250, 244)",
        "maskContent": '""',
    }
    assert abs(top_after - top_before) <= 1


def test_chromium_remix_create_button_does_not_shift_when_status_renders(page: BrowserPage) -> None:
    page.evaluate("document.querySelector('.remixButton').click()")
    page.wait_for_selector(".remixCreate")
    top_before = page.evaluate("document.querySelector('.remixCreate').getBoundingClientRect().top")
    top_after = page.evaluate(
        """new Promise((resolve) => {
          const dock = document.querySelector('.remixStatusDock');
          dock.innerHTML = '<div class="remixGeneratingPanel" role="status"><strong class="remixGeneratingWord">Generating</strong></div>';
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              resolve(document.querySelector('.remixCreate').getBoundingClientRect().top);
            });
          });
        })""",
        await_promise=True,
    )

    assert abs(top_after - top_before) <= 1


def test_chromium_remix_result_image_lightbox_fits_viewport(page: BrowserPage) -> None:
    page.evaluate("document.querySelector('.remixButton').click()")
    page.wait_for_selector(".remixPane")
    state = page.evaluate(
        """new Promise((resolve) => {
          const pane = document.querySelector('.remixPane');
          const source = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(`
            <svg xmlns="http://www.w3.org/2000/svg" width="2400" height="1600">
              <rect width="2400" height="1600" fill="#184f45"/>
              <text x="1200" y="820" text-anchor="middle" font-size="140" fill="#fff">Generated remix</text>
            </svg>
          `);
          const result = document.createElement('section');
          result.className = 'remixResult';
          result.innerHTML = `
            <div class="remixResultMedia">
              <button class="remixResultImageButton" type="button" aria-label="Open generated remix full size">
                <img src="${source}" alt="">
                <span class="remixResultZoomHint" aria-hidden="true"></span>
              </button>
            </div>
          `;
          pane.append(result);

          const closeLightbox = () => document.querySelector('.remixLightbox')?.remove();
          const openLightbox = () => {
            const lightbox = document.createElement('div');
            lightbox.className = 'remixLightbox';
            lightbox.setAttribute('role', 'dialog');
            lightbox.setAttribute('aria-modal', 'true');
            lightbox.innerHTML = `
              <button class="iconButton remixLightboxClose" type="button" aria-label="Close generated remix preview"></button>
              <img class="remixLightboxImage" src="${source}" alt="Generated remix full size">
            `;
            lightbox.addEventListener('click', (event) => {
              if (event.target === lightbox) closeLightbox();
            });
            lightbox.querySelector('.remixLightboxClose').addEventListener('click', closeLightbox);
            window.addEventListener('keydown', (event) => {
              if (event.key === 'Escape') closeLightbox();
            }, { once: true });
            document.body.append(lightbox);
          };
          result.querySelector('.remixResultImageButton').addEventListener('click', openLightbox);
          result.querySelector('.remixResultImageButton').click();

          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              const lightbox = document.querySelector('.remixLightbox');
              const image = document.querySelector('.remixLightboxImage');
              const button = document.querySelector('.remixResultImageButton');
              const box = lightbox.getBoundingClientRect();
              const imageBox = image.getBoundingClientRect();
              const buttonStyle = getComputedStyle(button);
              const imageStyle = getComputedStyle(image);
              const lightboxPosition = getComputedStyle(lightbox).position;
              const cursor = buttonStyle.cursor;
              const objectFit = imageStyle.objectFit;
              window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
              resolve({
                role: lightbox.getAttribute('role'),
                cursor,
                lightboxPosition,
                objectFit,
                fitsWidth: imageBox.width <= box.width,
                fitsHeight: imageBox.height <= box.height,
                closedAfterEscape: document.querySelector('.remixLightbox') === null,
              });
            });
          });
        })""",
        await_promise=True,
    )

    assert state == {
        "role": "dialog",
        "cursor": "zoom-in",
        "lightboxPosition": "fixed",
        "objectFit": "contain",
        "fitsWidth": True,
        "fitsHeight": True,
        "closedAfterEscape": True,
    }


def test_chromium_remix_attempt_list_is_single_row_clip_rail(page: BrowserPage) -> None:
    page.evaluate("document.querySelector('.remixButton').click()")
    page.wait_for_selector(".remixPane")
    state = page.evaluate(
        """new Promise((resolve) => {
          const pane = document.querySelector('.remixPane');
          const panel = document.createElement('section');
          panel.className = 'remixAttemptPanel';
          panel.innerHTML = `
            <div class="remixAttemptList">
              <div class="remixAttemptTrack">
                ${Array.from({ length: 8 }, (_, index) => `
                  <div class="remixAttempt ${index === 7 ? 'running' : 'failed'}">
                    <span>${index + 1}</span>
                    <div>
                      <strong>Attempt ${index + 1}</strong>
                      <p>${index === 7 ? 'Calling Gemini' : 'Safety filtered'}</p>
                    </div>
                  </div>
                `).join('')}
              </div>
            </div>
          `;
          pane.append(panel);
          const list = panel.querySelector('.remixAttemptList');
          const track = panel.querySelector('.remixAttemptTrack');
          requestAnimationFrame(() => {
            const offset = Math.max(0, track.scrollWidth - list.clientWidth);
            track.style.transition = 'none';
            track.style.transform = `translateX(-${offset}px)`;
            requestAnimationFrame(() => {
              const style = getComputedStyle(list);
              const trackStyle = getComputedStyle(track);
              const cards = Array.from(track.querySelectorAll('.remixAttempt')).map((card) =>
                card.getBoundingClientRect()
              );
              const listBox = list.getBoundingClientRect();
              resolve({
                display: style.display,
                trackDisplay: trackStyle.display,
                trackFlexWrap: trackStyle.flexWrap,
                trackTransform: trackStyle.transform,
                overflowX: style.overflowX,
                maskImage: style.maskImage || style.webkitMaskImage,
                rowSpread: Math.max(...cards.map((card) => card.top)) - Math.min(...cards.map((card) => card.top)),
                trackScrollWidth: track.scrollWidth,
                clientWidth: list.clientWidth,
                firstLeft: cards[0].left,
                listLeft: listBox.left,
                lastRight: cards[cards.length - 1].right,
                listRight: listBox.right,
              });
            });
          });
        })""",
        await_promise=True,
    )

    assert state["display"] == "block"
    assert state["trackDisplay"] == "flex"
    assert state["trackFlexWrap"] == "nowrap"
    assert state["overflowX"] == "hidden"
    assert state["trackScrollWidth"] > state["clientWidth"]
    assert state["trackTransform"] != "none"
    assert state["firstLeft"] < state["listLeft"]
    assert state["lastRight"] <= state["listRight"] + 1
    assert state["rowSpread"] <= 1
    assert state["maskImage"] != "none"


def test_chromium_remix_prompt_mode_sliders_cycle_without_growing_tweak_panel(page: BrowserPage) -> None:
    page.wait_for_selector(".itemCard")
    page.evaluate("document.querySelector('.itemCard')?.click()")
    page.evaluate("document.querySelector('.remixButton').click()")
    page.wait_for_selector(".remixPromptModeColumns")
    page.wait_for_selector(".schizoButton")
    page.wait_for_selector(".remixInstructionChecks")
    page.wait_for_selector(".chronicallyOnlineButton")

    instruction_initial = page.evaluate(
        """(() => {
          const schizo = document.querySelector('.schizoButton').getBoundingClientRect();
          const checks = document.querySelector('.remixInstructionChecks');
          const checkBox = checks.getBoundingClientRect();
          const checkRows = Array.from(checks.children).map((child) => child.getBoundingClientRect());
          const online = document.querySelector('.chronicallyOnlineButton').getBoundingClientRect();
          return {
            labels: Array.from(checks.querySelectorAll('span')).map((label) => label.textContent.trim()),
            checked: Array.from(checks.querySelectorAll('input')).map((input) => input.checked),
            disabled: Array.from(checks.querySelectorAll('input')).map((input) => input.disabled),
            stacked: checkRows[0].bottom <= checkRows[1].top && checkRows[1].bottom <= checkRows[2].top,
            betweenSliders: schizo.right <= checkBox.left && checkBox.right <= online.left,
            balancedSliders: Math.abs(schizo.width - online.width) <= 1,
          };
        })()"""
    )

    def state(selector: str) -> dict[str, Any]:
        return page.evaluate(
            f"""(() => {{
              const panel = document.querySelector('.remixTweakPanel');
              const slider = document.querySelector({json.dumps(selector)});
              const input = slider?.querySelector('input[type=\"range\"]');
              return {{
                height: panel.getBoundingClientRect().height,
                text: slider.querySelector('strong').textContent.trim(),
                level: slider.querySelector('.remixPromptModeValue').textContent.trim(),
                classes: Array.from(slider.classList),
                fill: slider.style.getPropertyValue('--slider-fill').trim(),
                thumb: slider.style.getPropertyValue('--slider-thumb').trim(),
                legacyTrackBorder: slider.style.getPropertyValue('--slider-track-border').trim(),
                legacyThumbShadow: slider.style.getPropertyValue('--slider-thumb-shadow').trim(),
                value: input ? Number(input.value) : null,
              }};
            }})()"""
        )

    def set_state(selector: str, value: int) -> dict[str, Any]:
        page.evaluate(
            f"""new Promise((resolve) => {{
              const slider = document.querySelector({json.dumps(selector)});
              const input = slider?.querySelector('input[type=\"range\"]');
              if (!input) {{
                resolve(null);
                return;
              }}
              const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
              setter.call(input, String({value}));
              input.dispatchEvent(new Event('input', {{ bubbles: true }}));
              input.dispatchEvent(new Event('change', {{ bubbles: true }}));
              requestAnimationFrame(() => {{
                resolve(true);
              }});
            }})""",
            await_promise=True,
        )
        return page.evaluate(
            f"""new Promise((resolve) => {{
              const panel = document.querySelector('.remixTweakPanel');
              const slider = document.querySelector({json.dumps(selector)});
              const input = slider?.querySelector('input[type=\"range\"]');
              requestAnimationFrame(() => {{
                requestAnimationFrame(() => {{
                  resolve({{
                    height: panel.getBoundingClientRect().height,
                    text: slider.querySelector('strong').textContent.trim(),
                    level: slider.querySelector('.remixPromptModeValue').textContent.trim(),
                    classes: Array.from(slider.classList),
                    fill: slider.style.getPropertyValue('--slider-fill').trim(),
                    thumb: slider.style.getPropertyValue('--slider-thumb').trim(),
                    legacyTrackBorder: slider.style.getPropertyValue('--slider-track-border').trim(),
                    legacyThumbShadow: slider.style.getPropertyValue('--slider-thumb-shadow').trim(),
                    value: input ? Number(input.value) : null,
                  }});
                }});
              }});
            }})""",
            await_promise=True,
        )

    schizo_initial = state(".schizoButton")
    schizo_one = set_state(".schizoButton", 1)
    schizo_two = set_state(".schizoButton", 2)
    schizo_three = set_state(".schizoButton", 3)
    schizo_returned = set_state(".schizoButton", 0)
    online_initial = state(".chronicallyOnlineButton")
    online_one = set_state(".chronicallyOnlineButton", 1)
    online_two = set_state(".chronicallyOnlineButton", 2)
    online_three = set_state(".chronicallyOnlineButton", 3)
    online_returned = set_state(".chronicallyOnlineButton", 0)
    page.evaluate(
        """(() => {
          const inputs = document.querySelectorAll('.remixInstructionChecks input');
          inputs[0].click();
          inputs[2].click();
        })()"""
    )
    instruction_checked = page.evaluate(
        """(() => ({
          checked: Array.from(document.querySelectorAll('.remixInstructionChecks input')).map((input) => input.checked),
          height: document.querySelector('.remixTweakPanel').getBoundingClientRect().height,
        }))()"""
    )
    heights = [
        schizo_initial["height"],
        schizo_one["height"],
        schizo_two["height"],
        schizo_three["height"],
        schizo_returned["height"],
        online_initial["height"],
        online_one["height"],
        online_two["height"],
        online_three["height"],
        online_returned["height"],
        instruction_checked["height"],
    ]

    assert instruction_initial["labels"] == ["Text Free", "Anti-Slop", "Comic Strip"]
    assert instruction_initial["checked"] == [False, False, False]
    assert instruction_initial["disabled"] == [False, False, False]
    assert instruction_initial["stacked"] is True
    assert instruction_initial["betweenSliders"] is True
    assert instruction_initial["balancedSliders"] is True
    assert instruction_checked["checked"] == [True, False, True]
    assert schizo_initial["text"] == "Schizophrenic"
    assert schizo_initial["level"] == "off"
    assert "level-0" in schizo_initial["classes"]
    assert schizo_initial["value"] == 0
    assert schizo_one["level"] == "low"
    assert "level-1" in schizo_one["classes"]
    assert schizo_one["value"] == 1
    assert schizo_two["level"] == "medium"
    assert "level-2" in schizo_two["classes"]
    assert schizo_two["value"] == 2
    assert schizo_three["level"] == "high"
    assert "level-3" in schizo_three["classes"]
    assert schizo_three["value"] == 3
    assert schizo_returned["level"] == "off"
    assert "level-0" in schizo_returned["classes"]
    assert schizo_returned["value"] == 0
    assert online_initial["text"] == "Chronically online"
    assert online_initial["level"] == "off"
    assert "level-0" in online_initial["classes"]
    assert online_initial["value"] == 0
    assert online_one["level"] == "low"
    assert "level-1" in online_one["classes"]
    assert online_one["value"] == 1
    assert online_two["level"] == "medium"
    assert "level-2" in online_two["classes"]
    assert online_two["value"] == 2
    assert online_three["level"] == "high"
    assert "level-3" in online_three["classes"]
    assert online_three["value"] == 3
    assert online_returned["level"] == "off"
    assert "level-0" in online_returned["classes"]
    assert online_returned["value"] == 0
    assert max(heights) - min(heights) <= 1
    assert schizo_two["thumb"].startswith("hsl(")
    assert schizo_two["fill"].startswith("hsl(")
    assert "/ 0.2)" in schizo_two["fill"]
    assert schizo_two["legacyTrackBorder"] == ""
    assert schizo_two["legacyThumbShadow"] == ""
    assert online_three["thumb"].startswith("hsl(")
    assert online_three["fill"].startswith("hsl(")
    assert "/ 0.2)" in online_three["fill"]
    assert online_three["legacyTrackBorder"] == ""
    assert online_three["legacyThumbShadow"] == ""


def test_chromium_remix_random_scope_tracks_contact_sheet_filters(page: BrowserPage) -> None:
    page.evaluate("document.querySelector('.remixButton').click()")
    page.wait_for_selector(".remixPane")

    initial = page.evaluate(
        """() => {
          return Array.from(document.querySelectorAll('.remixRandomAction')).map((button) => ({
            hasFilterBadge: Boolean(button.querySelector('.randomFilterBadge')),
            hasFilteredClass: button.classList.contains('hasScopeBadge'),
          }));
        }"""
    )
    assert all(not button["hasFilterBadge"] for button in initial)
    assert all(not button["hasFilteredClass"] for button in initial)

    page.evaluate(
        """(() => {
          const input = document.querySelector('.searchBox input');
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
          setter.call(input, 'hover video');
          input.dispatchEvent(new Event('input', { bubbles: true }));
        })()"""
    )
    page.evaluate("new Promise((resolve) => setTimeout(resolve, 450))", await_promise=True)

    filtered = page.evaluate(
        """() => {
          return Array.from(document.querySelectorAll('.remixRandomAction')).map((button) => ({
            hasFilterBadge: Boolean(button.querySelector('.randomFilterBadge')),
            hasFilteredClass: button.classList.contains('hasScopeBadge'),
          }));
        }"""
    )
    assert all(button["hasFilterBadge"] for button in filtered)
    assert all(button["hasFilteredClass"] for button in filtered)

    page.evaluate(
        """(() => {
          const input = document.querySelector('.searchBox input');
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
          setter.call(input, '');
          input.dispatchEvent(new Event('input', { bubbles: true }));
        })()"""
    )
    page.evaluate("new Promise((resolve) => setTimeout(resolve, 350))", await_promise=True)

    cleared = page.evaluate(
        """() => {
          return Array.from(document.querySelectorAll('.remixRandomAction')).map((button) => ({
            hasFilterBadge: Boolean(button.querySelector('.randomFilterBadge')),
            hasFilteredClass: button.classList.contains('hasScopeBadge'),
          }));
        }"""
    )
    assert all(not button["hasFilterBadge"] for button in cleared)
    assert all(not button["hasFilteredClass"] for button in cleared)

    page.evaluate(
        """(() => {
          document.querySelector('.filterRailButton')?.click();
          const mediaSelect = document.querySelector('.filters .field select');
          const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
          setter.call(mediaSelect, 'video');
          mediaSelect.dispatchEvent(new Event('change', { bubbles: true }));
        })()"""
    )
    page.evaluate("new Promise((resolve) => setTimeout(resolve, 250))", await_promise=True)

    filtered_by_filter = page.evaluate(
        """() => {
          return Array.from(document.querySelectorAll('.remixRandomAction')).map((button) => ({
            hasFilterBadge: Boolean(button.querySelector('.randomFilterBadge')),
            hasFilteredClass: button.classList.contains('hasScopeBadge'),
          }));
        }"""
    )
    assert all(button["hasFilterBadge"] for button in filtered_by_filter)
    assert all(button["hasFilteredClass"] for button in filtered_by_filter)

    page.evaluate("""(() => {
      const reset = document.querySelector('.filters .secondaryButton');
      reset?.click();
    })()""")
    page.evaluate("new Promise((resolve) => setTimeout(resolve, 250))", await_promise=True)

    returned = page.evaluate(
        """() => {
          return Array.from(document.querySelectorAll('.remixRandomAction')).map((button) => ({
            hasFilterBadge: Boolean(button.querySelector('.randomFilterBadge')),
            hasFilteredClass: button.classList.contains('hasScopeBadge'),
          }));
        }"""
    )
    assert all(not button["hasFilterBadge"] for button in returned)
    assert all(not button["hasFilteredClass"] for button in returned)


def test_chromium_video_thumbnail_hover_plays_and_resets(page: BrowserPage) -> None:
    rect = page.scroll_selector_to_center(".mediaPreview.videoPreview")
    page.send(
        "Input.dispatchMouseEvent",
        {
            "type": "mouseMoved",
            "x": rect["x"],
            "y": rect["y"],
            "button": "none",
            "pointerType": "mouse",
        },
    )
    page.evaluate("new Promise((resolve) => setTimeout(resolve, 1600))", await_promise=True)
    after_hover = page.evaluate(
        """(() => {
          const card = document.querySelector('.mediaPreview.videoPreview');
          const video = card.querySelector('video.hoverVideo');
          const poster = card.querySelector('.previewImage');
          const badge = card.querySelector('.playBadge');
          return {
            className: card.className,
            paused: video.paused,
            currentTime: Number(video.currentTime.toFixed(2)),
            readyState: video.readyState,
            videoOpacity: getComputedStyle(video).opacity,
            posterOpacity: getComputedStyle(poster).opacity,
            badgeOpacity: Number(getComputedStyle(badge).opacity)
          };
        })()"""
    )
    assert "is-previewing" in after_hover["className"]
    assert after_hover["paused"] is False
    assert after_hover["currentTime"] > 0.4
    assert after_hover["readyState"] >= 2
    assert after_hover["videoOpacity"] == "1"
    assert after_hover["posterOpacity"] == "0"
    assert after_hover["badgeOpacity"] > 0.6

    page.send(
        "Input.dispatchMouseEvent",
        {"type": "mouseMoved", "x": 1, "y": 1, "button": "none", "pointerType": "mouse"},
    )
    page.evaluate("new Promise((resolve) => setTimeout(resolve, 350))", await_promise=True)
    after_leave = page.evaluate(
        """(() => {
          const card = document.querySelector('.mediaPreview.videoPreview');
          const video = card.querySelector('video.hoverVideo');
          return {
            className: card.className,
            paused: video.paused,
            currentTime: Number(video.currentTime.toFixed(2)),
            videoOpacity: getComputedStyle(video).opacity
          };
        })()"""
    )
    assert "is-previewing" not in after_leave["className"]
    assert after_leave["paused"] is True
    assert after_leave["currentTime"] == 0
    assert after_leave["videoOpacity"] == "0"


def test_chromium_video_play_badge_toggles_preview(page: BrowserPage) -> None:
    rect = page.scroll_selector_to_center(".mediaPreview.videoPreview .playBadge")
    page.send(
        "Input.dispatchMouseEvent",
        {
            "type": "mouseMoved",
            "x": rect["x"],
            "y": rect["y"],
            "button": "none",
            "pointerType": "mouse",
        },
    )
    page.evaluate("new Promise((resolve) => setTimeout(resolve, 150))", await_promise=True)
    page.send(
        "Input.dispatchMouseEvent",
        {
            "type": "mousePressed",
            "x": rect["x"],
            "y": rect["y"],
            "button": "left",
            "clickCount": 1,
            "pointerType": "mouse",
        },
    )
    page.send(
        "Input.dispatchMouseEvent",
        {
            "type": "mouseReleased",
            "x": rect["x"],
            "y": rect["y"],
            "button": "left",
            "clickCount": 1,
            "pointerType": "mouse",
        },
    )
    page.evaluate("new Promise((resolve) => setTimeout(resolve, 1600))", await_promise=True)
    playing = page.evaluate(
        """(() => {
          const card = document.querySelector('.mediaPreview.videoPreview');
          const video = card.querySelector('video.hoverVideo');
          const badge = card.querySelector('.playBadge');
          return {
            className: card.className,
            paused: video.paused,
            currentTime: Number(video.currentTime.toFixed(2)),
            src: video.getAttribute('src') || '',
            manualSrc: video.dataset.manualSrc || '',
            muted: video.muted,
            loop: video.loop,
            controls: video.controls,
            disablePictureInPicture: video.disablePictureInPicture,
            disableRemotePlayback: video.disableRemotePlayback,
            pointerEvents: getComputedStyle(video).pointerEvents,
            videoOpacity: getComputedStyle(video).opacity,
            hasPlayButton: Boolean(badge)
          };
        })()"""
    )
    assert "is-previewing" in playing["className"]
    assert "is-manual-previewing" in playing["className"]
    assert playing["paused"] is False
    assert playing["currentTime"] > 0.4
    assert playing["src"].endswith("/media/video-playback/playback-card.mp4")
    assert playing["manualSrc"].endswith("/media/video-playback/playback-card.mp4")
    assert playing["muted"] is False
    assert playing["loop"] is False
    assert playing["controls"] is True
    assert playing["disablePictureInPicture"] is True
    assert playing["disableRemotePlayback"] is True
    assert playing["pointerEvents"] == "auto"
    assert playing["videoOpacity"] == "1"
    assert playing["hasPlayButton"] is False

    page.evaluate(
        "document.querySelector('.mediaPreview.videoPreview video.hoverVideo').dispatchEvent(new Event('ended'))"
    )
    page.evaluate("new Promise((resolve) => setTimeout(resolve, 350))", await_promise=True)
    stopped = page.evaluate(
        """(() => {
          const card = document.querySelector('.mediaPreview.videoPreview');
          const video = card.querySelector('video.hoverVideo');
          return {
            className: card.className,
            paused: video.paused,
            currentTime: Number(video.currentTime.toFixed(2)),
            videoOpacity: getComputedStyle(video).opacity,
            src: video.getAttribute('src') || '',
            controls: video.controls
          };
        })()"""
    )
    assert "is-previewing" not in stopped["className"]
    assert stopped["paused"] is True
    assert stopped["currentTime"] == 0
    assert stopped["videoOpacity"] == "0"
    assert stopped["src"] == ""
    assert stopped["controls"] is False


def test_chromium_mobile_video_tap_starts_one_unmuted_original_play(mobile_page: BrowserPage) -> None:
    mobile_page.send("Emulation.setTouchEmulationEnabled", {"enabled": True})
    rect = mobile_page.scroll_selector_to_center(".mediaPreview.videoPreview .playBadge")
    mobile_page.evaluate(
        """(() => {
          const video = document.querySelector('.mediaPreview.videoPreview video.hoverVideo');
          const nativeLoad = video.load.bind(video);
          const nativePlay = video.play.bind(video);
          window.__mobileVideoCalls = {load: 0, play: 0};
          video.load = () => {
            window.__mobileVideoCalls.load += 1;
            return nativeLoad();
          };
          video.play = () => {
            window.__mobileVideoCalls.play += 1;
            return nativePlay();
          };
        })()"""
    )
    mobile_page.send(
        "Input.dispatchTouchEvent",
        {
            "type": "touchStart",
            "touchPoints": [{"x": rect["x"], "y": rect["y"]}],
        },
    )
    mobile_page.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    mobile_page.evaluate("new Promise((resolve) => setTimeout(resolve, 1600))", await_promise=True)
    state = mobile_page.evaluate(
        """(() => {
          const card = document.querySelector('.mediaPreview.videoPreview');
          const video = card.querySelector('video.hoverVideo');
          return {
            calls: window.__mobileVideoCalls,
            className: card.className,
            src: video.getAttribute('src') || '',
            muted: video.muted,
            controls: video.controls,
            autoplay: video.autoplay,
            preload: video.preload,
            disablePictureInPicture: video.disablePictureInPicture,
            disableRemotePlayback: video.disableRemotePlayback,
            hasPlayButton: Boolean(card.querySelector('.playBadge')),
            paused: video.paused,
            currentTime: Number(video.currentTime.toFixed(2))
          };
        })()"""
    )
    assert state["calls"] == {"load": 1, "play": 1}
    assert "is-manual-previewing" in state["className"]
    assert state["src"].endswith("/media/video-playback/playback-card.mp4")
    assert state["muted"] is False
    assert state["controls"] is True
    assert state["autoplay"] is True
    assert state["preload"] == "auto"
    assert state["disablePictureInPicture"] is True
    assert state["disableRemotePlayback"] is True
    assert state["hasPlayButton"] is False
    assert state["paused"] is False
    assert state["currentTime"] > 0.4


def test_chromium_mobile_video_stall_uses_animated_preview(mobile_page: BrowserPage) -> None:
    mobile_page.send("Emulation.setTouchEmulationEnabled", {"enabled": True})
    rect = mobile_page.scroll_selector_to_center(".mediaPreview.videoPreview .playBadge")
    mobile_page.evaluate(
        """(() => {
          const video = document.querySelector('.mediaPreview.videoPreview video.hoverVideo');
          window.__videoPreviewWarnings = [];
          console.warn = (...args) => window.__videoPreviewWarnings.push(args.map(String).join(' '));
          const nativeGetAttribute = video.getAttribute.bind(video);
          const nativeSetAttribute = video.setAttribute.bind(video);
          const nativeLoad = video.load.bind(video);
          const nativePlay = video.play.bind(video);
          let blockedSource = '';
          video.getAttribute = (name) => name === 'src' && blockedSource ? blockedSource : nativeGetAttribute(name);
          video.setAttribute = (name, value) => {
            if (name === 'src' && String(value).includes('/media/video-playback/')) {
              blockedSource = String(value);
              return;
            }
            if (name === 'src') blockedSource = '';
            nativeSetAttribute(name, value);
          };
          video.load = () => {
            const source = video.getAttribute('src') || '';
            if (!source.includes('/media/video-playback/')) nativeLoad();
          };
          video.play = () => {
            const source = video.getAttribute('src') || '';
            if (source.includes('/media/video-playback/')) return new Promise(() => {});
            return nativePlay();
          };
        })()"""
    )
    mobile_page.send(
        "Input.dispatchTouchEvent",
        {
            "type": "touchStart",
            "touchPoints": [{"x": rect["x"], "y": rect["y"]}],
        },
    )
    mobile_page.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    mobile_page.evaluate("new Promise((resolve) => setTimeout(resolve, 200))", await_promise=True)
    after_tap = mobile_page.evaluate(
        """(() => {
          const card = document.querySelector('.mediaPreview.videoPreview');
          const video = card.querySelector('video.hoverVideo');
          return { className: card.className, src: video.getAttribute('src') || '', paused: video.paused };
        })()"""
    )
    assert "is-manual-previewing" in after_tap["className"], after_tap
    mobile_page.evaluate(
        "document.querySelector('.mediaPreview.videoPreview video.hoverVideo').dispatchEvent(new Event('stalled'))"
    )
    mobile_page.evaluate("new Promise((resolve) => setTimeout(resolve, 3900))", await_promise=True)
    state = mobile_page.evaluate(
        """(() => {
          const card = document.querySelector('.mediaPreview.videoPreview');
          const video = card.querySelector('video.hoverVideo');
          return {
            className: card.className,
            src: video.getAttribute('src') || '',
            muted: video.muted,
            loop: video.loop,
            controls: video.controls,
            paused: video.paused,
            currentTime: Number(video.currentTime.toFixed(2)),
            hasPlayButton: Boolean(card.querySelector('.playBadge')),
            hasPlaybackError: Boolean(card.querySelector('.playbackError')),
            warnings: window.__videoPreviewWarnings
          };
        })()"""
    )

    assert "is-previewing" in state["className"], state["warnings"]
    assert "is-manual-previewing" not in state["className"]
    assert state["src"].endswith("/media/video-previews/hover-card.mp4")
    assert state["muted"] is True
    assert state["loop"] is True
    assert state["controls"] is False
    assert state["paused"] is False
    assert state["currentTime"] > 0.2
    assert state["hasPlayButton"] is True
    assert state["hasPlaybackError"] is False


def test_chromium_mobile_layout_is_compact_two_column_and_light_on_media(mobile_page: BrowserPage) -> None:
    mobile_page.evaluate("new Promise((resolve) => setTimeout(resolve, 350))", await_promise=True)
    state = mobile_page.evaluate(
        """(() => {
          const rect = (selector) => {
            const node = document.querySelector(selector);
            if (!node) return null;
            const box = node.getBoundingClientRect();
            return {
              left: Math.round(box.left),
              right: Math.round(box.right),
              width: Math.round(box.width),
              height: Math.round(box.height)
            };
          };
          const cards = Array.from(document.querySelectorAll(".itemCard")).slice(0, 4).map((card) => {
            const box = card.getBoundingClientRect();
            return {left: Math.round(box.left), width: Math.round(box.width)};
          });
          const columns = new Set(cards.map((card) => card.left)).size;
          const resources = performance.getEntriesByType("resource").map((entry) => entry.name);
          return {
            viewportWidth: window.innerWidth,
            documentWidth: document.documentElement.scrollWidth,
            topbar: rect(".topbar"),
            content: rect(".content"),
            gallery: rect(".galleryGrid"),
            columns,
            cardCount: document.querySelectorAll(".itemCard").length,
            bottomNavVisible: Boolean(document.querySelector(".mobileActionBar")),
            resourceCount: resources.length,
            hoverVideoRequests: resources.filter((name) => name.includes("/media/video-previews/")).length,
            hoverVideoSrc: document.querySelector(".mediaPreview.videoPreview video.hoverVideo")?.getAttribute("src") || ""
          };
        })()"""
    )
    assert state["topbar"]["height"] <= 140
    assert state["content"]["left"] <= 12
    assert state["documentWidth"] <= state["viewportWidth"]
    assert state["columns"] == 2
    assert state["cardCount"] >= 2
    assert state["bottomNavVisible"] is True
    assert state["resourceCount"] <= 40
    assert state["hoverVideoRequests"] == 0
    assert state["hoverVideoSrc"] == ""


def test_chromium_mobile_gallery_density_breakpoints(chromium_dashboard: str) -> None:
    for width, expected_columns in [(430, 2), (330, 1)]:
        with chromium_page(chromium_dashboard, width=width, height=840, mobile=True) as browser_page:
            browser_page.wait_for_selector(".itemCard")
            state = browser_page.evaluate(
                """(() => {
                  const cards = Array.from(document.querySelectorAll(".itemCard")).slice(0, 4).map((card) => {
                    const box = card.getBoundingClientRect();
                    return {left: Math.round(box.left), width: Math.round(box.width)};
                  });
                  return {
                    width: window.innerWidth,
                    topbarHeight: Math.round(document.querySelector(".topbar").getBoundingClientRect().height),
                    contentLeft: Math.round(document.querySelector(".content").getBoundingClientRect().left),
                    docWidth: document.documentElement.scrollWidth,
                    columns: new Set(cards.map((card) => card.left)).size
                  };
                })()"""
            )
            assert state["width"] == width
            assert state["topbarHeight"] <= 140
            assert state["contentLeft"] <= 12
            assert state["docWidth"] <= width
            assert state["columns"] == expected_columns


def test_chromium_mobile_layout_switches_between_browse_and_detail_panels(mobile_page: BrowserPage) -> None:
    initial = mobile_page.evaluate(
        """(() => ({
          galleryVisible: Boolean(document.querySelector(".galleryGrid")),
          detailVisible: Boolean(document.querySelector(".detailPanelWrapper")),
          backButton: document.querySelector(".mobileBackButton")?.textContent?.trim() || "",
          bottomNav: Boolean(document.querySelector(".mobileActionBar")),
          itemCount: document.querySelectorAll(".itemCard").length
        }))()"""
    )
    assert initial["galleryVisible"] is True
    assert initial["detailVisible"] is False
    assert initial["backButton"] == ""
    assert initial["bottomNav"] is True
    assert initial["itemCount"] >= 1

    mobile_page.evaluate("document.querySelector('.itemCard')?.click()")
    mobile_page.wait_for_selector(".detailPanelWrapper")
    after_select = mobile_page.evaluate(
        """(() => {
          const rect = (selector) => {
            const box = document.querySelector(selector).getBoundingClientRect();
            return {
              top: Math.round(box.top),
              right: Math.round(box.right),
              bottom: Math.round(box.bottom),
              left: Math.round(box.left),
              height: Math.round(box.height)
            };
          };
          const detail = document.querySelector(".mobileDetailWrapper .detail");
          return {
            galleryVisible: Boolean(document.querySelector(".galleryGrid")),
            detailVisible: Boolean(document.querySelector(".detailPanelWrapper")),
            backButton: document.querySelector(".mobileBackButton")?.textContent?.trim() || "",
            width: window.innerWidth,
            mobile: window.matchMedia('(max-width: 760px)').matches,
            topbar: rect(".topbar"),
            workspace: rect(".workspace"),
            wrapper: rect(".mobileDetailWrapper"),
            nav: rect(".mobileActionBar"),
            detailOverflowY: getComputedStyle(detail).overflowY,
            detailHeight: Math.round(detail.getBoundingClientRect().height)
          };
        })()"""
    )
    assert after_select["galleryVisible"] is False
    assert after_select["detailVisible"] is True
    assert after_select["backButton"] == "Back to gallery"
    assert after_select["mobile"] is True
    assert after_select["width"] <= 760
    assert after_select["workspace"]["top"] >= after_select["topbar"]["bottom"] - 1
    assert after_select["workspace"]["bottom"] <= after_select["nav"]["top"] + 1
    assert after_select["wrapper"]["bottom"] <= after_select["nav"]["top"] + 1
    assert after_select["detailOverflowY"] in {"auto", "scroll"}
    assert after_select["detailHeight"] > 0
    mobile_page.evaluate(
        """(() => {
          const detail = document.querySelector(".mobileDetailWrapper .detail");
          detail.scrollTop = detail.scrollHeight;
        })()"""
    )
    unobscured = mobile_page.evaluate(
        """(() => {
          const nav = document.querySelector(".mobileActionBar").getBoundingClientRect();
          const detail = document.querySelector(".mobileDetailWrapper .detail").getBoundingClientRect();
          const lastAction = Array.from(document.querySelectorAll(".mobileDetailWrapper .detailActions > *")).at(-1);
          const actionBox = lastAction.getBoundingClientRect();
          return {
            actionBottom: Math.round(actionBox.bottom),
            detailBottom: Math.round(detail.bottom),
            navTop: Math.round(nav.top)
          };
        })()"""
    )
    assert unobscured["actionBottom"] <= unobscured["navTop"]
    assert unobscured["detailBottom"] <= unobscured["navTop"]

    mobile_page.evaluate("document.querySelector('.mobileBackButton').click()")
    mobile_page.evaluate("new Promise((resolve) => setTimeout(resolve, 220))", await_promise=True)
    restored = mobile_page.evaluate(
        """(() => ({
          detailVisible: Boolean(document.querySelector('.detailPanelWrapper')),
          galleryVisible: Boolean(document.querySelector('.galleryGrid')),
          backButton: document.querySelector('.mobileBackButton')?.textContent?.trim() || ''
        }))()"""
    )
    assert restored["detailVisible"] is False
    assert restored["galleryVisible"] is True
    assert restored["backButton"] == ""


def test_chromium_mobile_back_to_gallery_restores_card_position(mobile_page: BrowserPage) -> None:
    before = mobile_page.evaluate(
        """(() => {
          const style = document.createElement('style');
          style.textContent = '.galleryGrid { padding-top: 1100px !important; }';
          document.head.appendChild(style);
          const card = document.querySelector('.itemCard');
          card.scrollIntoView({ block: 'center' });
          return {
            itemId: card.dataset.itemId,
            scrollY: window.scrollY,
            cardTop: card.getBoundingClientRect().top
          };
        })()"""
    )
    assert before["scrollY"] > 500

    mobile_page.evaluate(f"document.querySelector('.itemCard[data-item-id=\"{before['itemId']}\"]').click()")
    mobile_page.wait_for_selector(".detailPanelWrapper")
    mobile_page.evaluate("document.querySelector('.mobileBackButton').click()")
    mobile_page.wait_for_selector(".galleryGrid")
    mobile_page.evaluate(
        "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))",
        await_promise=True,
    )
    restored = mobile_page.evaluate(
        f"""(() => {{
          const card = document.querySelector('.itemCard[data-item-id="{before['itemId']}"]');
          return {{
            scrollY: window.scrollY,
            cardTop: card.getBoundingClientRect().top
          }};
        }})()"""
    )

    assert abs(restored["scrollY"] - before["scrollY"]) <= 2
    assert abs(restored["cardTop"] - before["cardTop"]) <= 2


def test_chromium_mobile_filter_sheet_open_close(mobile_page: BrowserPage) -> None:
    mobile_page.evaluate("document.querySelector('.mobileActionBar .filterActive')?.click()")
    mobile_page.wait_for_selector(".filters.is-open")
    opened = mobile_page.evaluate(
        """(() => {
          const sheet = document.querySelector('.filters');
          const header = document.querySelector('.filtersHeader');
          const firstField = document.querySelector('.filtersBody .field');
          const topbar = document.querySelector('.topbar');
          const rect = (node) => {
            const box = node.getBoundingClientRect();
            return {top: Math.round(box.top), bottom: Math.round(box.bottom), left: Math.round(box.left), right: Math.round(box.right)};
          };
          return {
            open: Boolean(document.querySelector('.filters.is-open')),
            sheet: rect(sheet),
            header: rect(header),
            firstField: rect(firstField),
            topbar: rect(topbar),
            docWidth: document.documentElement.scrollWidth,
            viewportWidth: window.innerWidth
          };
        })()"""
    )
    assert opened["open"] is True
    assert opened["sheet"]["left"] >= 0
    assert opened["sheet"]["right"] <= opened["viewportWidth"]
    assert opened["header"]["top"] >= opened["sheet"]["top"]
    assert opened["firstField"]["top"] >= opened["header"]["bottom"]
    assert opened["firstField"]["top"] > opened["topbar"]["bottom"]
    assert opened["docWidth"] <= opened["viewportWidth"]
    mobile_page.evaluate("document.querySelector('.scrim')?.click()")
    mobile_page.evaluate("new Promise((resolve) => setTimeout(resolve, 220))", await_promise=True)
    assert mobile_page.evaluate("Boolean(document.querySelector('.filters.is-open'))") is False


def test_chromium_mobile_remix_panel_stays_within_viewport(mobile_page: BrowserPage) -> None:
    mobile_page.evaluate("document.querySelector('.mobileActionBar .remixActive')?.click()")
    mobile_page.wait_for_selector(".remixPane")
    state = mobile_page.evaluate(
        """(() => {
          const overflow = Array.from(document.querySelectorAll(".remixPane, .remixPane *")).filter((node) => {
            if (!(node instanceof Element)) return false;
            const box = node.getBoundingClientRect();
            if (!box.width || !box.height) return false;
            const style = getComputedStyle(node);
            if (style.display === "none" || style.visibility === "hidden") return false;
            return box.left < -1 || box.right > window.innerWidth + 1;
          }).map((node) => {
            const box = node.getBoundingClientRect();
            return {
              selector: node.className || node.tagName.toLowerCase(),
              left: Math.round(box.left),
              right: Math.round(box.right),
              text: (node.textContent || "").trim().slice(0, 40)
            };
          });
          return {
            docWidth: document.documentElement.scrollWidth,
            viewportWidth: window.innerWidth,
            collapsedTweaks: Boolean(document.querySelector(".remixTweakPanel.is-collapsed")),
            centerRows: getComputedStyle(document.querySelector(".remixCenter")).gridTemplateRows,
            centerHeight: Math.round(document.querySelector(".remixCenter").getBoundingClientRect().height),
            centerExtra: Math.round(
              document.querySelector(".remixCenter").getBoundingClientRect().height -
              document.querySelector(".remixActionGroup").getBoundingClientRect().height -
              (document.querySelector(".remixStatusDock")?.getBoundingClientRect().height || 0)
            ),
            statusDockPresent: Boolean(document.querySelector(".remixStatusDock")),
            statusDockText: document.querySelector(".remixStatusDock")?.textContent.trim() || "",
            remixEmptyMinHeight: getComputedStyle(document.querySelector(".remixEmpty")).minHeight,
            overflow
          };
        })()"""
    )
    assert state["docWidth"] <= state["viewportWidth"]
    assert state["collapsedTweaks"] is True
    assert state["centerRows"].endswith("3px")
    assert state["centerHeight"] <= 112
    assert state["centerExtra"] <= 18
    assert state["statusDockPresent"] is False
    assert state["statusDockText"] == ""
    assert state["remixEmptyMinHeight"] == "22px"
    assert state["overflow"] == []


def test_chromium_mobile_analyze_scrolls_to_tweaks(mobile_page: BrowserPage) -> None:
    mobile_page.evaluate(
        """(() => {
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const url = String(typeof input === "string" ? input : input.url);
            if (url.endsWith("/health")) {
              return Promise.resolve(new Response(JSON.stringify({
                enabled: true,
                provider: "google",
                model: "test-remix",
                aspect_ratio: "1:1",
                image_size: "1024",
                message: "Ready"
              }), { status: 200, headers: { "Content-Type": "application/json" } }));
            }
            if (url.endsWith("/analysis")) {
              return new Promise((resolve) => setTimeout(() => {
                resolve(new Response(JSON.stringify({
                  prompt_version: "test-analysis-v1",
                  source: "gemini",
                  summary: "Mobile analysis ready",
                  controls: [
                    {
                      id: "contrast",
                      label: "Contrast",
                      description: "Push the source tension.",
                      low_label: "Subtle",
                      high_label: "Loud",
                      default_value: 64
                    },
                    {
                      id: "absurdity",
                      label: "Absurdity",
                      description: "Raise the weirdness.",
                      low_label: "Grounded",
                      high_label: "Unhinged",
                      default_value: 58
                    }
                  ]
                }), { status: 200, headers: { "Content-Type": "application/json" } }));
              }, 80));
            }
            return originalFetch(input, init);
          };
        })()"""
    )
    mobile_page.evaluate("document.querySelector('.mobileActionBar .remixActive')?.click()")
    mobile_page.wait_for_selector(".remixPane")
    mobile_page.evaluate("document.querySelectorAll('.itemCard')[0]?.querySelector('[data-remix-side=\"left\"]')?.click()")
    mobile_page.wait_for_selector(".remixPane .remixSource")
    mobile_page.evaluate("document.querySelectorAll('.itemCard')[1]?.querySelector('[data-remix-side=\"right\"]')?.click()")
    mobile_page.evaluate(
        """new Promise((resolve, reject) => {
          const started = Date.now();
          const tick = () => {
            const analyze = document.querySelector(".remixAnalyze");
            if (document.querySelectorAll(".remixSource").length === 2 && analyze && !analyze.disabled) {
              resolve(true);
              return;
            }
            if (Date.now() - started > 2500) {
              reject(new Error("Timed out waiting for enabled Analyze"));
              return;
            }
            setTimeout(tick, 50);
          };
          tick();
        })""",
        await_promise=True,
    )
    scrolled = mobile_page.evaluate(
        """new Promise((resolve) => {
          const beforeY = window.scrollY;
          document.querySelector(".remixAnalyze").click();
          const started = Date.now();
          const tick = () => {
            const panel = document.querySelector(".remixTweakPanel");
            const topbar = document.querySelector(".topbar");
            const summary = panel?.querySelector(".remixTweakSummary")?.textContent || "";
            const panelBox = panel?.getBoundingClientRect();
            const topbarBox = topbar.getBoundingClientRect();
            const scrolledIntoPlace =
              panelBox && panelBox.top >= topbarBox.bottom - 2 && panelBox.top <= topbarBox.bottom + 56;
            if (panel && summary && !panel.classList.contains("is-collapsed") && (scrolledIntoPlace || Date.now() - started > 2500)) {
              resolve({
                beforeY,
                afterY: window.scrollY,
                panelTop: Math.round(panelBox.top),
                topbarBottom: Math.round(topbarBox.bottom),
                collapsed: panel.classList.contains("is-collapsed"),
                summary
              });
              return;
            }
            if (Date.now() - started > 2500) {
              resolve({
                beforeY,
                afterY: window.scrollY,
                panelTop: 0,
                topbarBottom: Math.round(topbar.getBoundingClientRect().bottom),
                collapsed: panel?.classList.contains("is-collapsed") ?? true,
                summary
              });
              return;
            }
            requestAnimationFrame(tick);
          };
          tick();
        })""",
        await_promise=True,
    )
    assert scrolled["summary"] == "Mobile analysis ready"
    assert scrolled["collapsed"] is False
    assert scrolled["afterY"] >= scrolled["beforeY"] + 24
    assert scrolled["panelTop"] >= scrolled["topbarBottom"] - 2
    reset = mobile_page.evaluate(
        """new Promise((resolve) => {
          document.querySelectorAll('.itemCard')[1]?.querySelector('[data-remix-side="left"]')?.click();
          setTimeout(() => {
            resolve({
              summary: document.querySelector(".remixTweakSummary")?.textContent || "",
              status: document.querySelector(".remixTweakStatus")?.textContent || "",
              sources: document.querySelectorAll(".remixSource").length
            });
          }, 120);
        })""",
        await_promise=True,
    )
    assert reset["summary"] == ""
    assert reset["status"] == ""
    assert reset["sources"] == 1


def test_chromium_mobile_card_remix_button_seeds_remix_slots(mobile_page: BrowserPage) -> None:
    initial = mobile_page.evaluate(
        """(() => {
          const buttons = Array.from(document.querySelectorAll(".itemRemixButton"));
          const firstButton = buttons[0]?.getBoundingClientRect();
          const firstCard = document.querySelector(".itemCard")?.getBoundingClientRect();
          return {
            buttonCount: buttons.length,
            labels: buttons.slice(0, 2).map((button) => button.textContent.trim()),
            buttonHeight: Math.round(firstButton?.height || 0),
            buttonBottom: Math.round(firstButton?.bottom || 0),
            cardBottom: Math.round(firstCard?.bottom || 0),
            detailVisible: Boolean(document.querySelector(".detailPanelWrapper"))
          };
        })()"""
    )
    assert initial["buttonCount"] >= 4
    assert initial["labels"] == ["Remix A", "Remix B"]
    assert 30 <= initial["buttonHeight"] <= 36
    assert initial["buttonBottom"] <= initial["cardBottom"]
    assert initial["detailVisible"] is False

    mobile_page.evaluate(
        """(() => {
          document.body.style.minHeight = "1800px";
          window.scrollTo(0, 420);
        })()"""
    )
    mobile_page.evaluate("document.querySelectorAll('.itemCard')[0]?.querySelector('[data-remix-side=\"right\"]')?.click()")
    mobile_page.wait_for_selector(".remixPane .remixSource")
    first_seed = mobile_page.evaluate(
        """(() => {
          const pane = document.querySelector(".remixPane")?.getBoundingClientRect();
          const topbar = document.querySelector(".topbar")?.getBoundingClientRect();
          const slots = Array.from(document.querySelectorAll(".remixSlot")).map((slot) => ({
            label: slot.querySelector(".remixSlotHeader strong")?.textContent || "",
            hasSource: Boolean(slot.querySelector(".remixSource")),
            text: slot.querySelector(".remixSource")?.textContent.toLowerCase() || ""
          }));
          return {
            detailVisible: Boolean(document.querySelector(".detailPanelWrapper")),
            remixOpen: Boolean(document.querySelector(".remixPane")),
            sources: document.querySelectorAll(".remixSource").length,
            activeNav: document.querySelector(".mobileActionBar .remixActive")?.classList.contains("active"),
            scrollY: window.scrollY,
            paneTop: Math.round(pane?.top || 0),
            topbarBottom: Math.round(topbar?.bottom || 0),
            confirmed: document.querySelectorAll(".itemCard")[0]?.querySelector('[data-remix-side="right"]')?.classList.contains("is-confirmed"),
            slots
          };
        })()"""
    )
    assert first_seed["detailVisible"] is False
    assert first_seed["remixOpen"] is True
    assert first_seed["sources"] == 1
    assert first_seed["activeNav"] is True
    assert not (first_seed["topbarBottom"] - 2 <= first_seed["paneTop"] <= first_seed["topbarBottom"] + 56)
    assert first_seed["confirmed"] is True
    assert first_seed["slots"][0]["hasSource"] is False
    assert "static dashboard image" in first_seed["slots"][1]["text"]

    mobile_page.evaluate("document.querySelectorAll('.itemCard')[1]?.querySelector('[data-remix-side=\"right\"]')?.click()")
    mobile_page.evaluate("new Promise((resolve) => setTimeout(resolve, 120))", await_promise=True)
    replaced_seed = mobile_page.evaluate(
        """(() => {
          const slots = Array.from(document.querySelectorAll(".remixSlot")).map((slot) => ({
            hasSource: Boolean(slot.querySelector(".remixSource")),
            text: slot.querySelector(".remixSource")?.textContent.toLowerCase() || ""
          }));
          return {
            sources: document.querySelectorAll(".remixSource").length,
            slots
          };
        })()"""
    )
    assert replaced_seed["sources"] == 1
    assert replaced_seed["slots"][0]["hasSource"] is False
    assert "hover video preview target" in replaced_seed["slots"][1]["text"]

    mobile_page.evaluate("document.querySelectorAll('.itemCard')[0]?.querySelector('[data-remix-side=\"left\"]')?.click()")
    mobile_page.evaluate("new Promise((resolve) => setTimeout(resolve, 120))", await_promise=True)
    second_seed = mobile_page.evaluate(
        """(() => {
          const slots = Array.from(document.querySelectorAll(".remixSlot")).map((slot) => ({
            hasSource: Boolean(slot.querySelector(".remixSource")),
            text: slot.querySelector(".remixSource")?.textContent.toLowerCase() || ""
          }));
          return {
            sources: document.querySelectorAll(".remixSource").length,
            createDisabled: document.querySelector(".remixCreate")?.disabled,
            slots
          };
        })()"""
    )
    assert second_seed["sources"] == 2
    assert "static dashboard image" in second_seed["slots"][0]["text"]
    assert "hover video preview target" in second_seed["slots"][1]["text"]
