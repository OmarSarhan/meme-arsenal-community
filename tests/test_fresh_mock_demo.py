"""Real CLI/static React acceptance; run via verify_fresh_mock_demo.sh in CI."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

import pytest

pytestmark = pytest.mark.chromium
ROOT = Path(__file__).resolve().parents[1]


def test_fresh_mock_demo(tmp_path):
    # Do not use shared settings/fixture_dir/page: init must create its own DB,
    # without the owner row or synthetic data supplied by those fixtures.
    assert sys.platform == "linux" and sys.version_info[:2] == (3, 12)
    for tool in ("node", "npm", "ffmpeg", "arsenal"):
        assert shutil.which(tool), f"Required acceptance tool missing: {tool}"
    version = subprocess.check_output(["node", "--version"], text=True, timeout=10)
    major, minor, *_ = map(int, version.strip().lstrip("v").split("."))
    assert major == 22 and minor >= 12
    from playwright.sync_api import expect, sync_playwright

    browser_path = os.environ["PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"]
    assert os.access(browser_path, os.X_OK)
    assert (ROOT / "frontend/dashboard/node_modules").is_dir()
    home = tmp_path / "home"
    home.mkdir()
    reserve = tmp_path / "reserve"
    assert not reserve.exists()
    env = {
        "PATH": os.environ["PATH"], "HOME": str(home), "LANG": "C.UTF-8",
        "RESERVE_PATH": str(reserve),
        "DATABASE_URL": f"sqlite:///{reserve}/data/arsenal.sqlite",
        "AI_PROVIDER": "mock",
    }
    arsenal = str(Path(sys.executable).parent / "arsenal")

    def cli(*args):
        print("CLI:", "arsenal", *args, flush=True)
        result = subprocess.run(
            [arsenal, *args], cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=240,
        )
        print(result.stdout)
        assert result.returncode == 0

    cli("init")
    init_report = json.loads(next(reserve.rglob("*-init.json")).read_text())
    assert init_report["database"] == "sqlite"
    assert init_report["database_url"] == env["DATABASE_URL"]
    cli("sync", "--fixture-dir", "tests/fixtures", "--limit", "10")
    reports = list(reserve.rglob("*-sync.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    print("Sync evidence:", json.dumps(report, sort_keys=True))
    assert report["import"]["media"] == 2
    for field in ("processed", "classified", "embedded"):
        assert report["pipeline"][field] == 2
    for field in ("process_failed", "classify_failed", "embed_failed"):
        assert report["pipeline"][field] == 0
    assert report["catalog"]["item_count"] == 2
    assert report["dashboard"]["mode"] == "vite"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}/dashboard/dist/"
    with (tmp_path / "server.log").open("w+") as log:
        server = subprocess.Popen(
            [arsenal, "serve-dashboard", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                assert server.poll() is None, "Dashboard server exited"
                try:
                    with urlopen(base, timeout=1) as response:
                        html = response.read().decode()
                    break
                except OSError:
                    assert time.monotonic() < deadline, "Dashboard readiness timed out"
                    time.sleep(0.1)
            assert 'id="root"' in html and 'type="module"' in html
            assert "fallback.js" not in html and "fallback.css" not in html
            modules = re.findall(r'<script[^>]+src="([^"]+)"', html)
            assert modules
            for module in modules:
                with urlopen(urljoin(base, module), timeout=5) as response:
                    assert response.status == 200
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=browser_path, headless=True, args=["--no-sandbox"],
                )
                context = None
                unexpected = []
                statuses = {}
                failures = []
                try:
                    context = browser.new_context(service_workers="block")
                    context.set_default_timeout(15000)

                    def route_request(route):
                        parsed = urlparse(route.request.url)
                        if parsed.scheme in ("http", "https") and parsed.netloc == f"127.0.0.1:{port}":
                            route.continue_()
                        else:
                            unexpected.append(f"{parsed.scheme}://{parsed.netloc}")
                            route.abort()

                    context.route("**/*", route_request)
                    page = context.new_page()
                    page.on("response", lambda r: statuses.__setitem__(r.url, r.status))
                    page.on("requestfailed", lambda r: failures.append({
                        "url": r.url, "failure": r.failure,
                    }))
                    page.goto(base, wait_until="domcontentloaded", timeout=30000)
                    cards = page.locator("#root .itemCard")
                    expect(cards).to_have_count(2)
                    expect(page.locator(".sectionBar strong")).to_have_text("2 of 2 shown")
                    expect(page.locator(".errorState")).to_have_count(0)
                    config_url = urljoin(base, "arsenal-config.json")
                    assert statuses[config_url] == 200
                    config = context.request.get(config_url).json()
                    assert "apiBase" not in config
                    assert config["catalogIndex"] == "../../catalog/index.json"
                    index_url = urljoin(base, config["catalogIndex"])
                    assert statuses[index_url] == 200
                    index = context.request.get(index_url).json()
                    items = []
                    for path in index["chunks"]:
                        url = urljoin(base, path)
                        assert statuses[url] == 200
                        items.extend(context.request.get(url).json()["items"])
                    tags_url = urljoin(base, index["tags_path"])
                    assert statuses[tags_url] == 200
                    assert context.request.get(tags_url).status == 200
                    manifest = json.loads((ROOT / "tests/fixtures/manifest.json").read_text())
                    assert {item["caption"] for item in items} == {
                        message["caption"] for message in manifest["messages"]
                    }
                    deploy = next(item for item in items if "deploy" in item["caption"])
                    for i in range(2):
                        image = cards.nth(i).locator("img.previewImage")
                        image.scroll_into_view_if_needed()
                        expect(image).to_be_visible()
                        page.wait_for_function(
                            "img => img.complete && img.naturalWidth > 0", arg=image.element_handle(),
                        )
                        assert urlparse(image.get_attribute("src")).path.endswith(".jpg")
                    # No CLI search bundle was exported: exercise lexical fallback.
                    search = page.get_by_placeholder("Search archive")
                    search.fill("deploy")
                    expect(cards).to_have_count(1)
                    expect(cards.first).to_have_attribute("data-item-id", str(deploy["id"]))
                    search.fill("")
                    expect(cards).to_have_count(2)
                    page.locator(f'.itemCard[data-item-id="{deploy["id"]}"]').click()
                    detail = page.locator(".detail:visible")
                    expect(detail).to_contain_text(deploy["caption"])
                    expect(detail.locator(".sourceBox")).to_contain_text("Deploy failed reaction")
                    expect(detail.locator(f'a[href="{deploy["source_url"]}"]')).to_be_visible()
                    preview = detail.locator(".detailMedia img")
                    page.wait_for_function(
                        "img => img.complete && img.naturalWidth > 0", arg=preview.element_handle(),
                    )
                    health = context.request.get(urljoin(base, "../../api/remix/health"))
                    assert health.status == 200
                    assert health.json()["provider"] == "mock"
                    assert health.json()["enabled"] is False
                    assert not unexpected
                    assert server.poll() is None
                finally:
                    print("Browser unexpected external requests:", unexpected)
                    print("Browser request failures:", json.dumps(failures, sort_keys=True))
                    print("Response evidence:", json.dumps(statuses, sort_keys=True))
                    if context is not None:
                        context.close()
                    browser.close()
        finally:
            if server.poll() is None:
                server.send_signal(signal.SIGINT)
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=10)
            log.seek(0)
            print("Server log:", log.read())
