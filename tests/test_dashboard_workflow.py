"""Mobile/desktop workflow checks against deterministic, unpaid API responses."""

from functools import partial
from dataclasses import replace
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import os
import re
import shutil
import subprocess
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageDraw
from fastapi.testclient import TestClient
import pytest

from meme_arsenal.remix_page import render_remix_page
from meme_arsenal.online import create_app, refresh_public_indexes
from meme_arsenal.db import session_scope
from meme_arsenal.media import process_pending_media
from meme_arsenal.telegram_importer import import_fixture_messages
from test_dashboard_chromium import _catalog_item


playwright = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.chromium
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def workflow_url():
    dist = Path(os.environ.get("DASHBOARD_TEST_DIST", ROOT / "frontend/dashboard/dist"))
    if not (dist / "index.html").exists():
        pytest.fail("Build the dashboard before running browser tests: npm --prefix frontend/dashboard run build")
    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            # Browser downloads bypass Playwright page routing.
            if self.path == "/fixture/79.png":
                buffer = BytesIO()
                Image.new("RGB", (100, 146), "#d1eadb").save(buffer, "PNG")
                body = buffer.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

    handler = partial(Handler, directory=str(dist))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture(params=[320, 390, 768, 1440], ids=["small-mobile", "mobile", "tablet", "desktop"])
def workflow(request, workflow_url):
    width = request.param
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, args=["--no-sandbox"], executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
        context = browser.new_context(viewport={"width": width, "height": 900}, is_mobile=width < 760)
        # Only the disposable fixture server may receive browser traffic.
        context.route("**/*", lambda route: route.continue_()
                      if route.request.url.startswith(f"{workflow_url}/") else route.abort())
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        sample = Image.new("RGB", (480, 360), "#d1eadb")
        draw = ImageDraw.Draw(sample)
        draw.rectangle((50, 50, 220, 310), fill="#e6b0c2")
        draw.text((245, 145), "MEME ARSENAL\nTEST MEDIA", fill="#173828", font_size=24)
        buffer = BytesIO()
        sample.save(buffer, "PNG")
        picture = buffer.getvalue()
        items = [_catalog_item(item_id=i, media_type="image", summary=f"Meme {i}", preview_path=f"/fixture/{i}.png") for i in range(80, 0, -1)]
        items[0]["archived"] = True
        for item in items:
            item.update(origin="telegram", public_status="published", visibility="public")
        items[3].update(classification_status="blocked", classification_block_reason="OTHER", summary="A blocked meme with a longer title that wraps across several lines")
        state = {"items": items, "job": None, "polls": 0, "save_count": 0, "share_count": 0, "share_active": False, "analysis_count": 0, "analysis_fail": False, "save_fail": False, "delete_count": 0, "delete_fail": False}
        analysis = {"prompt_version": "test-v1", "summary": "Combine the two sources", "source": "gemini", "controls": [
            {"id": "chaos", "label": "Chaos", "description": "", "low_label": "Calm", "high_label": "Wild", "default_value": 50},
            {"id": "contrast", "label": "Contrast", "description": "", "low_label": "Gentle", "high_label": "Bold", "default_value": 40},
        ]}

        def route_api(route):
            request = route.request
            url = urlsplit(request.url)
            path = url.path
            query = parse_qs(url.query)
            payload = {}
            status = 200
            if path == "/api/items":
                shown = [item for item in state["items"] if query.get("include_hidden") == ["true"] or not item["archived"]]
                q = query.get("q", [""])[0]
                if q:
                    shown = [item for item in shown if q.lower() in item["summary"].lower()]
                payload = {"items": shown, "total": len(shown), "next_cursor": None}
            elif path == "/api/catalog-state":
                payload = {"item_count": len(state["items"]), "revision": "fixed"}
            elif (path.endswith("/remix-card") or path.endswith("/card")) and query.get("page") == ["true"]:
                response = render_remix_page(
                    image_url="/fixture/79.png",
                    sources=[("Meme 79", "/fixture/79.png"), ("Meme 78", "/fixture/78.png")],
                    summary="Generated remix",
                    tweaks=state["job"]["tweaks"],
                )
                route.fulfill(body=response.body, headers=dict(response.headers))
                return
            elif path.endswith("/remix-card") or path.endswith("/image") or path.endswith("/card"):
                route.fulfill(body=picture, content_type="image/png")
                return
            elif path.endswith("/share"):
                if request.method == "POST":
                    state["share_count"] += 1
                    state["share_active"] = True
                    if query.get("card"):
                        state["recipe_active"] = True
                elif request.method == "DELETE":
                    state["share_active"] = False
                    state["recipe_active"] = False
                active = state.get("recipe_active", False) if query.get("card") else state["share_active"]
                payload = {"enabled": True, "variant": "recipe" if query.get("card") else "output", "active": active, "url": f"{workflow_url}/share/test-token" if active else None, "card_url": f"{workflow_url}/share/test-token/card" if active and query.get("card") else None, "media_url": f"{workflow_url}/share/test-token/media" if active else None}
            elif path == "/api/remix/health":
                payload = {"enabled": True, "provider": "mock"}
            elif path == "/api/remix/analysis":
                state["analysis_count"] += 1
                payload = analysis if not state["analysis_fail"] else {"detail": "Blocked"}
                status = 200 if not state["analysis_fail"] else 422
            elif path == "/api/remix/jobs" and request.method == "GET":
                payload = {"jobs": state.get("retained_jobs", [])}
            elif path == "/api/remix/jobs" and request.method == "POST":
                inputs = request.post_data_json
                job_id = "test-job" if state["job"] is None else state["job"]["id"] + "-next"
                state["job"] = {"id": job_id, **inputs, "status": "queued", "message": "Preparing source images", "attempts": [{"attempt": 1, "mitigation_id": "standard", "label": "Original request", "status": "running", "message": "Preparing source images"}]}
                state["polls"] = 0
                payload = state["job"]
            elif path.endswith("/save"):
                state["save_count"] += 1
                if state["save_fail"]:
                    payload = {"detail": "Test save failure"}
                    status = 503
                else:
                    saved = {**state["items"][1], "id": 100, "origin": "remix", "summary": "Generated remix", "remix_tweaks": state["job"]["tweaks"], "remix_sources": [{"id": i, "title": f"Meme {i}", "preview_path": f"/fixture/{i}.png"} for i in (79, 78)]}
                    state["job"].update(status="saved", saved_item=saved)
                    state["items"] = [saved, *[item for item in state["items"] if item["id"] != saved["id"]]]
                    payload = {"status": "saved", "item": saved}
            elif path == f"/api/remix/jobs/{(state['job'] or {}).get('id')}":
                state["polls"] += 1
                if state["polls"] >= 2 and state["job"]["status"] == "queued":
                    state["job"].update(status="ready", message="Ready", result={"summary": "Generated remix", "image_path": "test.png"})
                    state["job"]["attempts"][0].update(status="succeeded", message="Remix generated")
                payload = state["job"]
                if query.get("include_sources") == ["true"] and not state.get("local_backend"):
                    payload = {**payload, "source_items": state.get("source_items", [
                        item for item in state["items"] if item["id"] in (payload["left_id"], payload["right_id"])
                    ])}
            elif path == "/api/remix/items/100" and request.method == "DELETE":
                state["delete_count"] += 1
                if state["delete_fail"]:
                    status = 503
                    payload = {"detail": "Test delete failure"}
                else:
                    state["items"] = [item for item in state["items"] if item["id"] != 100]
                    state["job"]["status"] = "deleted"
                    route.fulfill(status=204)
                    return
            else:
                status = 404
            route.fulfill(status=status, json=payload)

        page.route("**/arsenal-config.json", lambda route: route.fulfill(json={"mode": "online", "apiBase": "/api", "remixApiBase": "/api/remix"}))
        page.route("**/api/**", route_api)
        page.route("**/fixture/**", lambda route: route.fulfill(body=picture, content_type="image/png"))
        page.goto(workflow_url)
        playwright.expect(page.locator(".itemCard")).to_have_count(79)
        yield page, state, width
        assert errors == []
        context.close()
        browser.close()


def select_pair(page):
    page.locator('.itemCard[data-item-id="79"]').get_by_role("button", name="Use as A", exact=True).click()
    playwright.expect(page.locator(".remixCornerA")).to_have_text("A")
    page.locator('.itemCard[data-item-id="78"]').get_by_role("button", name="Use as B", exact=True).click()
    playwright.expect(page.locator(".remixCornerB")).to_have_text("B")
    playwright.expect(page.locator(".remixPickerTray")).to_have_count(0)
    page.get_by_role("navigation").get_by_role("button", name="Remix", exact=True).click()


def test_retained_job_reselection_restores_sources_and_settings(workflow):
    page, state, _width = workflow
    state["job"] = {"id": "retained", "left_id": 79, "right_id": 78, "status": "ready",
                    "safety_mode": "safer", "result": {"summary": "Recovered", "image_path": "test.png"},
                    "tweaks": {"controls": [{"id": "chaos", "label": "Chaos", "description": "",
                                              "low_label": "Calm", "high_label": "Wild", "default_value": 50}],
                               "values": {"chaos": 75}, "no_text": True, "comic_strip": True,
                               "anti_slop": True, "schizo_level": 2, "chronically_online_level": 3}}
    state["retained_jobs"] = [state["job"]]
    draft = {**state["items"][3], "origin": "dashboard_upload", "public_status": "draft", "visibility": "private"}
    page.route("**/api/remix/uploads?*", lambda route: route.fulfill(json={"drafts": [draft]}))
    page.route(f"**/api/remix/uploads/{draft['id']}", lambda route: route.fulfill(json=draft))
    page.get_by_role("navigation").get_by_role("button", name="Remix", exact=True).click()
    page.get_by_role("button", name="Retained jobs", exact=True).click()
    picker = page.get_by_label("Retained jobs", exact=True)

    def restore():
        picker.select_option("retained")
        playwright.expect(page.locator(".remixResult")).to_be_visible()
        playwright.expect(page.locator('.remixSlot[aria-label="A"]')).to_contain_text("Meme 79")
        playwright.expect(page.locator('.remixSlot[aria-label="B"]')).to_contain_text("Meme 78")
        playwright.expect(page.get_by_label("Text Free", exact=True)).to_be_checked()
        playwright.expect(page.get_by_label("Comic Strip", exact=True)).to_be_checked()
        playwright.expect(page.get_by_label("Anti-Slop", exact=True)).to_be_checked()
        playwright.expect(page.get_by_role("button", name="Safer", exact=True)).to_have_attribute("aria-pressed", "true")
        playwright.expect(page.locator('.remixPromptModeSlider input[type="range"]').nth(0)).to_have_value("2")
        playwright.expect(page.locator('.remixPromptModeSlider input[type="range"]').nth(1)).to_have_value("3")
        playwright.expect(page.locator('.remixSlider input[max="100"]')).to_have_value("75")

    restore()
    page.get_by_text("Text Free", exact=True).click()
    playwright.expect(page.get_by_label("Text Free", exact=True)).not_to_be_checked()
    restore()  # Same ID must restore submitted settings even without clearing first.
    page.locator(".remixCancelResult").click()
    playwright.expect(page.locator(".remixResult")).to_have_count(0)
    restore()
    page.get_by_role("button", name="Uploaded drafts", exact=True).click()
    page.get_by_label("Uploaded drafts", exact=True).select_option(str(draft["id"]))
    playwright.expect(page.locator(".remixResult")).to_have_count(0)
    playwright.expect(page.locator('.remixSlot[aria-label="B"] .remixSource')).to_have_count(0)
    restore()
    state["source_items"] = []  # An online denial must never resurrect cached sources.
    picker.select_option("retained")
    playwright.expect(page.locator(".remixSource")).to_have_count(0)


def test_local_recovery_preserves_only_matching_cached_sources(workflow):
    page, state, _width = workflow
    state["local_backend"] = True
    state["job"] = {"id": "local-job", "left_id": 79, "right_id": 78, "status": "ready",
                    "result": {"summary": "Local recovery", "image_path": "test.png"}}
    page.route("**/arsenal-config.json", lambda route: route.fulfill(json={
        "mode": "online", "apiBase": "/api", "remixApiBase": "../../api/remix"}))
    workspace = {"left": state["items"][1], "right": state["items"][2], "jobId": "local-job"}
    for mismatch in (False, True):
        if mismatch:
            workspace["left"] = state["items"][3]
        page.evaluate("w => sessionStorage.setItem('meme-arsenal.remix.workspace.v1:../../api/remix', JSON.stringify(w))", workspace)
        page.reload()
        page.get_by_role("navigation").get_by_role("button", name="Remix", exact=True).click()
        playwright.expect(page.locator(".remixResult")).to_be_visible()
        playwright.expect(page.locator('.remixSlot[aria-label="B"]')).to_contain_text("Meme 78")
        if mismatch:
            playwright.expect(page.locator('.remixSlot[aria-label="A"] .remixSource')).to_have_count(0)
        else:
            playwright.expect(page.locator('.remixSlot[aria-label="A"]')).to_contain_text("Meme 79")


@pytest.mark.parametrize("media_kind", ["image", "video"])
def test_owner_recovery_across_browser_storage_loss(workflow_url, settings, fixture_dir, tmp_path, media_kind):
    if media_kind == "video" and not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    settings = replace(settings, public_session_cookie_secure=False, redis_url="redis://localhost:1/0")
    with session_scope(settings) as db:
        import_fixture_messages(db, settings, fixture_dir)
        process_pending_media(db, settings)
        refresh_public_indexes(db, settings=settings)
    app = create_app(settings)
    media = fixture_dir / "deploy-fails.png"
    if media_kind == "video":
        media = tmp_path / "draft.mp4"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=duration=1:size=64x64:rate=12", str(media)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Real owner API/database, independent browser cookies/storage, no provider or
    # proxy service. Actual Basic Auth/Origin protection is the community CI gate.
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, args=["--no-sandbox"], executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
        try:
            pages = []
            def wire_api(client):
                def route_api(route):
                    request = route.request
                    parsed = urlsplit(request.url)
                    client.cookies.clear()
                    response = client.request(request.method, parsed.path + ("?" + parsed.query if parsed.query else ""),
                                              headers={key: value for key, value in request.headers.items()
                                                       if key in ("content-type", "cookie", "range")},
                                              content=request.post_data_buffer)
                    route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
                return route_api
            for _ in range(2):
                context = browser.new_context(viewport={"width": 390, "height": 900})
                context.route("**/*", lambda route: route.continue_()
                              if route.request.url.startswith(f"{workflow_url}/") else route.abort())
                context.route("**/arsenal-config.json", lambda route: route.fulfill(json={
                    "mode": "online", "apiBase": "/api", "remixApiBase": "/api/remix"}))
                api = wire_api(TestClient(app))
                context.route("**/api/**", api)
                context.route("**/media/**", api)
                page = context.new_page()
                page.goto(workflow_url)
                playwright.expect(page.locator(".itemCard")).to_have_count(2)
                pages.append(page)
            first, second = pages
            item_id = first.locator(".itemCard").first.get_attribute("data-item-id")
            curation = {"favorite": True, "archived": True, "manual_tags": ["shared-owner"], "notes": "retained note"}
            assert first.evaluate("""async ({id, value}) => (await fetch(`/api/session/items/${id}`, {
                method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify(value)})).status""",
                                  {"id": item_id, "value": curation}) == 200
            draft = first.evaluate("""async ({bytes, name, type}) => {
                const body = new FormData(); body.append('file', new File([new Uint8Array(bytes)], name, {type}));
                const response = await fetch('/api/remix/uploads', {method:'POST', body});
                if (!response.ok) throw new Error(await response.text()); return response.json();
            }""", {"bytes": list(media.read_bytes()), "name": media.name,
                   "type": "video/mp4" if media_kind == "video" else "image/png"})
            for page in pages:
                page.context.clear_cookies()
                page.evaluate("sessionStorage.clear(); localStorage.clear()")
                page.reload()
                playwright.expect(page.locator(".itemCard")).to_have_count(1)
                detail = page.evaluate("async id => (await fetch(`/api/items/${id}`)).json()", item_id)
                assert {key: detail[key] for key in curation} == curation
                page.get_by_role("navigation").get_by_role("button", name="Remix", exact=True).click()
                page.get_by_role("button", name="Uploaded drafts", exact=True).click()
                page.get_by_label("Uploaded drafts", exact=True).select_option(str(draft["id"]))
                playwright.expect(page.locator('.remixSlot[aria-label="A"] .remixSource')).to_be_visible()
                assert page.evaluate("async id => (await fetch(`/api/remix/uploads/${id}`)).status", draft["id"]) == 200
            saved = second.evaluate("async id => (await fetch(`/api/remix/uploads/${id}/save`, {method:'POST'})).json()", draft["id"])
            assert saved["item"]["public_status"] == "published"
            first.reload()
            first.get_by_role("navigation").get_by_role("button", name="Gallery", exact=True).click()
            playwright.expect(first.locator(f'.itemCard[data-item-id="{draft["id"]}"]')).to_be_visible()
        finally:
            browser.close()


def analyze_and_generate(page):
    page.get_by_role("button", name="Analyze", exact=True).click()
    playwright.expect(page.locator('.remixSlider input[max="100"]')).to_have_count(2)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(ROOT / f"tmp/workflow-tweaks-{page.viewport_size['width']}.png"))
    slider = page.locator('.remixSlider input[max="100"]').first
    slider.focus()
    slider.press("End")
    page.get_by_role("button", name="Create Remix", exact=True).click()
    playwright.expect(page.locator(".generationOverlay")).to_be_visible()
    panel = page.locator(".generationOverlay .remixGeneratingPanel").bounding_box()
    word = page.locator(".generationOverlay .remixGeneratingWord").bounding_box()
    assert abs(panel["x"] + panel["width"] / 2 - word["x"] - word["width"] / 2) < 2
    assert abs(panel["y"] + panel["height"] / 2 - word["y"] - word["height"] / 2) < 2
    playwright.expect(page.locator(".generationStage")).to_contain_text("Preparing source images")
    page.screenshot(path=str(ROOT / f"tmp/workflow-generating-{page.viewport_size['width']}.png"))
    playwright.expect(page.locator(".remixResult")).to_be_visible(timeout=8000)
    playwright.expect(page.locator(".generationOverlay")).to_have_count(0)


def test_source_selection_hidden_and_navigation(workflow):
    page, state, width = workflow
    page.get_by_label("Include hidden").check()
    playwright.expect(page.locator(".itemCard")).to_have_count(80)
    playwright.expect(page.locator('.itemCard[data-item-id="80"]')).to_have_class(re.compile("is-hidden"))
    select_pair(page)
    a = page.locator('.remixSlot[aria-label="A"]').bounding_box()
    b = page.locator('.remixSlot[aria-label="B"]').bounding_box()
    assert abs(a["y"] - b["y"]) < 2
    assert a["x"] + a["width"] <= b["x"]
    page.get_by_role("button", name="Open a remix source details").click()
    if width < 760:
        page.get_by_role("button", name="Back to remix").click()
    else:
        page.get_by_role("navigation").get_by_role("button", name="Remix", exact=True).click()
    playwright.expect(page.locator('.remixSlot[aria-label="B"] .remixSource')).to_be_visible()
    page.get_by_role("navigation").get_by_role("button", name="Gallery", exact=True).click()
    card = page.locator('.itemCard[data-item-id="60"]')
    card.scroll_into_view_if_needed()
    page.wait_for_timeout(200)
    before = card.bounding_box()["y"]
    page.get_by_role("navigation").get_by_role("button", name="Remix", exact=True).click()
    page.get_by_role("navigation").get_by_role("button", name="Gallery", exact=True).click()
    page.wait_for_timeout(200)
    assert abs(card.bounding_box()["y"] - before) < 8
    nav = page.locator(".destinationNav").bounding_box()
    assert nav["x"] == 0 and nav["width"] == width
    assert nav["y"] + nav["height"] == 900
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(ROOT / f"tmp/workflow-gallery-{width}.png"))
    page.get_by_placeholder("Search archive").fill("Meme 80")
    playwright.expect(page.locator(".itemCard")).to_have_count(1)
    page.get_by_role("navigation").get_by_role("button", name="Remix", exact=True).click()
    playwright.expect(page.get_by_role("button", name=re.compile("Random A from"))).to_be_disabled()
    playwright.expect(page.get_by_role("button", name="Find similar for A")).to_have_count(0)


def test_remix_stages_save_and_share_snapshot(workflow):
    page, state, width = workflow
    select_pair(page)
    analyze_and_generate(page)
    assert state["job"]["tweaks"]["values"]["chaos"] == 100
    playwright.expect(page.locator('.remixSlider input[max="100"]').first).not_to_be_visible()
    actions = page.locator(".remixResultActions button")
    playwright.expect(actions).to_have_count(3)
    # Result arrival can scroll the whole row between separate browser calls.
    rows = actions.evaluate_all("buttons => buttons.map(button => button.getBoundingClientRect().y)")
    assert max(rows) - min(rows) < 2
    page.screenshot(path=str(ROOT / f"tmp/workflow-result-{width}.png"))
    page.get_by_role("button", name="Remix Page", exact=True).click()
    playwright.expect(page.locator(".remixPresentation")).to_be_visible()
    playwright.expect(page.frame_locator(".recipePreview").locator(".recipePage")).to_be_visible()
    assert state["save_count"] == 0
    assert page.locator(".remixPresentation a[download]").count() == 0
    page.get_by_role("button", name="Save to library", exact=True).click()
    publish = page.get_by_role("button", name="Publish page", exact=True)
    playwright.expect(publish).to_be_enabled()
    assert state["save_count"] == 1
    assert state["share_count"] == 0
    assert state["job"]["saved_item"]["remix_tweaks"]["values"]["chaos"] == 100
    page.evaluate("Object.defineProperty(navigator, 'share', {configurable: true, value: undefined}); Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {writeText: async text => {window.copiedLink = text;}}})")
    publish.click()
    playwright.expect(page.get_by_role("status").filter(has_text="Public link copied")).to_be_visible()
    link = page.get_by_role("textbox", name="Public share link")
    playwright.expect(link).to_have_value(re.compile("/share/test-token/card$"))
    assert page.evaluate("window.copiedLink") == link.input_value()
    page.evaluate("Object.defineProperty(navigator, 'share', {configurable: true, value: async data => {window.sharedLink = data.url;}})")
    page.get_by_role("button", name="Share page", exact=True).click()
    playwright.expect(page.get_by_role("status").filter(has_text="Shared")).to_be_visible()
    assert page.evaluate("window.sharedLink") == link.input_value()
    assert state["share_count"] == 1
    page.get_by_role("button", name="Revoke shares", exact=True).click()
    playwright.expect(publish).to_be_enabled()
    playwright.expect(link).to_have_count(0)
    assert not state["share_active"]
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Share media", exact=True).click()
    with page.expect_download() as downloaded:
        page.get_by_role("link", name="Download", exact=True).click()
    assert downloaded.value.suggested_filename == "remix-100.png"
    assert Path(downloaded.value.path()).read_bytes().startswith(b"\x89PNG")
    assert state["save_count"] == 1
    page.keyboard.press("Escape")
    playwright.expect(page.locator(".remixPresentation")).to_have_count(0)
    playwright.expect(page.get_by_role("button", name="Delete from Arsenal", exact=True)).to_be_enabled()
    playwright.expect(page.locator(".remixTasksControl")).to_have_count(0)
    playwright.expect(page.locator(".remixProgressSummary")).to_have_text("Generation complete")
    page.locator(".remixAttemptList summary").click()
    playwright.expect(page.locator(".remixAttemptTrack")).to_contain_text("Original request")
    playwright.expect(page.locator(".remixAttemptTrack")).to_contain_text("Remix generated")
    page.get_by_role("button", name="Analyze", exact=True).click()
    playwright.expect(page.locator('.remixSlider input[max="100"]')).to_have_count(2)
    assert state["analysis_count"] == 2
    assert state["job"]["saved_item"]["remix_tweaks"]["values"]["chaos"] == 100


def test_analysis_failure_reopens_and_save_failure_is_retryable(workflow):
    page, state, width = workflow
    page.emulate_media(reduced_motion="reduce")
    select_pair(page)
    state["analysis_fail"] = True
    page.get_by_role("button", name="Analyze", exact=True).click()
    playwright.expect(page.get_by_text("Could not prepare tweak suggestions", exact=False)).to_be_visible()
    state["analysis_fail"] = False
    analyze_and_generate(page)
    state["save_fail"] = True
    page.get_by_role("button", name="Save to Arsenal", exact=True).click()
    playwright.expect(page.locator(".saveToast.is-error")).to_be_visible()
    state["save_fail"] = False
    page.get_by_role("navigation").get_by_role("button", name="Gallery", exact=True).click()
    page.get_by_role("button", name="Retry save", exact=True).click()
    playwright.expect(page.locator(".saveToast")).to_have_text(re.compile("Saved"))
    assert state["save_count"] == 2
    playwright.expect(page.locator(".remixPane")).to_have_count(0)


def test_saved_result_delete_and_scrap_are_distinct(workflow):
    page, state, width = workflow
    select_pair(page)
    analyze_and_generate(page)
    page.get_by_role("button", name="Save to Arsenal", exact=True).click()
    delete = page.get_by_role("button", name="Delete from Arsenal", exact=True)
    playwright.expect(delete).to_be_enabled()
    page.once("dialog", lambda dialog: dialog.dismiss())
    delete.click()
    assert state["delete_count"] == 0
    state["delete_fail"] = True
    page.once("dialog", lambda dialog: dialog.accept())
    delete.click()
    playwright.expect(page.locator(".saveToast.is-error")).to_contain_text("Test delete failure")
    playwright.expect(delete).to_be_enabled()
    playwright.expect(page.locator(".remixResult")).to_be_visible()
    state["delete_fail"] = False
    page.once("dialog", lambda dialog: dialog.accept())
    delete.click()
    playwright.expect(page.locator(".remixResult")).to_have_count(0)
    playwright.expect(page.locator(".remixPane")).to_be_visible()
    assert all(item["id"] != 100 for item in state["items"])
    page.get_by_role("navigation").get_by_role("button", name="Gallery", exact=True).click()
    playwright.expect(page.locator('.itemCard[data-item-id="100"]')).to_have_count(0)
    page.get_by_role("navigation").get_by_role("button", name="Remix", exact=True).click()
    analyze_and_generate(page)
    page.get_by_role("button", name="Save to Arsenal", exact=True).click()
    playwright.expect(delete).to_be_enabled()
    page.get_by_role("button", name="Scrap", exact=True).click()
    playwright.expect(page.locator(".remixResult")).to_have_count(0)
    assert state["delete_count"] == 2
    assert any(item["id"] == 100 for item in state["items"])
    page.get_by_role("navigation").get_by_role("button", name="Gallery", exact=True).click()
    playwright.expect(page.locator('.itemCard[data-item-id="100"]')).to_be_visible()


def test_card_footer_retry_and_upload_layout(workflow):
    page, state, width = workflow
    select_pair(page)
    for label in ("A", "B"):
        upload = page.get_by_role("button", name=f"Upload {label}", exact=True)
        box = upload.bounding_box()
        icon = upload.locator("svg").bounding_box()
        assert abs(box["x"] + box["width"] / 2 - icon["x"] - icon["width"] / 2) < 2
    page.get_by_role("navigation").get_by_role("button", name="Gallery", exact=True).click()
    card = page.locator('.itemCard[data-item-id="77"]')
    card.scroll_into_view_if_needed()
    box = card.bounding_box()
    actions = card.locator(".cardRemixActions").bounding_box()
    assert 0 <= box["y"] + box["height"] - actions["y"] - actions["height"] <= 2
    card.locator(".itemDetailButton").click()
    retry = page.get_by_role("button", name="Retry classification", exact=True)
    playwright.expect(retry).to_be_visible()
    assert retry.inner_text() == ""
    # The current block-notice style sets a 38px minimum over the 36px base width.
    assert retry.bounding_box()["width"] == 38
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(ROOT / f"tmp/workflow-blocked-{width}.png"))
