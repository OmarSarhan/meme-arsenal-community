from __future__ import annotations

import json
import shutil
import subprocess
import threading
import uuid
from contextlib import contextmanager
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib import error, request

from PIL import Image
import pytest

from meme_arsenal.remix_server import ArsenalDashboardHandler, RemixJobStore


@contextmanager
def served_dashboard(settings) -> Iterator[str]:
    jobs = RemixJobStore(settings)
    handler = partial(ArsenalDashboardHandler, settings=settings, jobs=jobs)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        jobs.shutdown()
        thread.join(timeout=5)


def fetch(base: str, path: str, *, method: str = "GET", headers: dict[str, str] | None = None) -> tuple[int, object, bytes]:
    try:
        http_request = request.Request(f"{base}{path}", method=method, headers=headers or {})
        with request.urlopen(http_request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _build_png(path: Path) -> bytes:
    image = Image.new("RGB", (80, 80), color=(33, 66, 99))
    image.save(path, format="PNG")
    return path.read_bytes()


def _build_mp4(path: Path) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required for local video upload test")
    command = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x90:rate=1",
        "-t",
        "1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(path),
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _multipart_post(
    base: str,
    path: str,
    *,
    filename: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> tuple[int, object, bytes]:
    boundary = f"----arsenal-{uuid.uuid4().hex}"
    payload = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        data,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    body = b"".join(payload)
    http_request = request.Request(
        f"{base}{path}",
        method="POST",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))},
    )
    try:
        with request.urlopen(http_request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_dashboard_server_serves_public_assets_with_security_headers(settings):
    (settings.dashboard_dist_dir / "index.html").write_text("<!doctype html>dashboard", encoding="utf-8")
    (settings.catalog_dir / "index.json").write_text('{"ok":true}', encoding="utf-8")
    (settings.previews_dir / "sample.txt").write_text("media", encoding="utf-8")

    with served_dashboard(settings) as base:
        status, headers, body = fetch(base, "/dashboard/dist/index.html")
        assert status == 200
        assert body == b"<!doctype html>dashboard"
        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert headers.get("X-Frame-Options") == "DENY"
        assert "frame-ancestors 'none'" in (headers.get("Content-Security-Policy") or "")

        status, headers, body = fetch(base, "/catalog/index.json")
        assert status == 200
        assert body == b'{"ok":true}'
        assert headers.get("Cross-Origin-Resource-Policy") == "same-origin"

        status, _, body = fetch(base, "/media/previews/sample.txt")
        assert status == 200
        assert body == b"media"

        status, headers, body = fetch(base, "/api/remix/health")
        assert status == 200
        assert b'"enabled":' in body
        assert headers.get("Cache-Control") == "no-store"

        status, headers, body = fetch(base, "/favicon.ico")
        assert status == 204
        assert body == b""
        assert headers.get("X-Content-Type-Options") == "nosniff"


def test_dashboard_server_supports_byte_ranges_for_public_media(settings):
    media = settings.originals_dir / "sample.mp4"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"0123456789")

    with served_dashboard(settings) as base:
        status, headers, body = fetch(base, "/media/originals/sample.mp4", headers={"Range": "bytes=2-5"})
        assert status == 206
        assert body == b"2345"
        assert headers.get("Accept-Ranges") == "bytes"
        assert headers.get("Content-Range") == "bytes 2-5/10"
        assert headers.get("Content-Length") == "4"

        status, headers, body = fetch(base, "/media/originals/sample.mp4", method="HEAD", headers={"Range": "bytes=-3"})
        assert status == 206
        assert body == b""
        assert headers.get("Content-Range") == "bytes 7-9/10"
        assert headers.get("Content-Length") == "3"

        status, headers, body = fetch(base, "/media/originals/sample.mp4", headers={"Range": "bytes=20-30"})
        assert status == 416
        assert body == b""
        assert headers.get("Content-Range") == "bytes */10"


def test_dashboard_server_blocks_private_reserve_paths_and_directory_indexes(settings):
    (settings.dashboard_dist_dir / "index.html").write_text("<!doctype html>dashboard", encoding="utf-8")
    (settings.dashboard_dist_dir / ".env").write_text("dashboard-secret", encoding="utf-8")
    (settings.data_dir / "telegram.session").write_text("session-secret", encoding="utf-8")
    (settings.data_dir / "arsenal.sqlite").write_text("sqlite-secret", encoding="utf-8")
    (settings.run_logs_dir / "latest.json").write_text("log-secret", encoding="utf-8")
    (settings.remix_drafts_dir / "draft.png").write_text("draft-secret", encoding="utf-8")

    blocked_paths = [
        "/data/telegram.session",
        "/data/arsenal.sqlite",
        "/data/run-logs/latest.json",
        "/data/remix-drafts/draft.png",
        "/dashboard/dist/.env",
        "/dashboard/dist/%2e%2e/%2e%2e/data/telegram.session",
        "/media/%2e%2e/data/telegram.session",
        "/catalog/",
        "/media/",
        "/api/remixevil",
    ]

    with served_dashboard(settings) as base:
        for path in blocked_paths:
            status, headers, body = fetch(base, path)
            assert status == 404, path
            assert b"secret" not in body
            assert headers.get("X-Content-Type-Options") == "nosniff"

        status, _, _ = fetch(base, "/data/telegram.session", method="HEAD")
        assert status == 404


def test_dashboard_server_uploads_and_saves_from_local_endpoint(settings, tmp_path: Path):
    with served_dashboard(settings) as base:
        image_path = tmp_path / "upload-check.png"
        image_data = _build_png(image_path)
        status, _, upload_body = _multipart_post(
            base,
            "/api/remix/uploads",
            filename="upload-check.png",
            data=image_data,
            content_type="image/png",
        )
        assert status == 200
        draft = json.loads(upload_body.decode("utf-8"))
        assert draft["origin"] == "dashboard_upload"
        assert draft["public_status"] == "draft"
        assert draft["visibility"] == "private"
        assert draft["processing_status"] == "processed"
        assert draft["original_path"] is not None

        status, _, save_body = fetch(
            base,
            f"/api/remix/uploads/{draft['id']}/save",
            method="POST",
        )
        assert status == 200
        saved = json.loads(save_body.decode("utf-8"))
        assert saved["status"] == "saved"
        assert saved["item"]["id"] == draft["id"]
        assert saved["item"]["public_status"] == "published"
        assert saved["item"]["visibility"] == "public"


def test_dashboard_server_uploads_video_from_local_endpoint(settings, tmp_path: Path):
    with served_dashboard(settings) as base:
        video_path = tmp_path / "upload-check.mp4"
        _build_mp4(video_path)
        status, _, upload_body = _multipart_post(
            base,
            "/api/remix/uploads",
            filename="upload-check.mp4",
            data=video_path.read_bytes(),
            content_type="video/mp4",
        )
        assert status == 200
        draft = json.loads(upload_body.decode("utf-8"))
        assert draft["origin"] == "dashboard_upload"
        assert draft["media_type"] == "video"
        assert draft["public_status"] == "draft"
        assert draft["visibility"] == "private"
        assert draft["processing_status"] == "processed"
        assert draft["original_path"] is not None

        status, _, save_body = fetch(
            base,
            f"/api/remix/uploads/{draft['id']}/save",
            method="POST",
        )
        assert status == 200
        saved = json.loads(save_body.decode("utf-8"))
        assert saved["status"] == "saved"
        assert saved["item"]["id"] == draft["id"]
        assert saved["item"]["public_status"] == "published"
        assert saved["item"]["visibility"] == "public"
