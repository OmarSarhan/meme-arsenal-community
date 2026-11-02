"""CI installed-dashboard path; runs only on the existing internal fixture network."""

from io import BytesIO
from urllib.parse import parse_qs, urlsplit

from PIL import Image
from playwright.sync_api import expect, sync_playwright


ORIGIN = "https://fixture.example.invalid"
unexpected = []
image = BytesIO()
Image.new("RGB", (40, 40), "purple").save(image, format="PNG")
assert len(image.getvalue()) < 1024 * 1024  # Documented first-use input bound.
with sync_playwright() as driver:
    browser = driver.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
    try:
        context = browser.new_context(http_credentials={"username": "fixture", "password": "community-fixture-password"},
                                      viewport={"width": 1440, "height": 1000}, service_workers="block")

        def restrict(route):
            if urlsplit(route.request.url).scheme + "://" + urlsplit(route.request.url).netloc == ORIGIN:
                route.continue_()
            else:
                unexpected.append(route.request.url)
                route.abort()

        context.route("**/*", restrict)
        page = context.new_page()
        page.set_default_timeout(20000)
        page.goto(ORIGIN, wait_until="networkidle", timeout=30000)
        page.get_by_role("navigation").get_by_role("button", name="Remix", exact=True).click()
        health = page.evaluate("""async () => {
            const response = await fetch('/api/remix/health');
            return {status: response.status, body: await response.json()};
        }""")
        assert health["status"] == 200
        assert health["body"]["provider"] == "mock" and health["body"]["enabled"] is False
        expect(page.get_by_role("button", name="Analyze", exact=True)).to_be_disabled()
        # The empty workspace has no Create control until both sources are selected.
        expect(page.get_by_role("button", name="Create Remix", exact=True)).to_have_count(0)
        page.locator('input[type="file"]').first.set_input_files({
            "name": "guide-purple.png", "mimeType": "image/png", "buffer": image.getvalue()})
        page.get_by_role("button", name="Open a remix source details", exact=True).click()
        with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/save")) as saved:
            page.get_by_role("button", name="Save to arsenal", exact=True).click()
        assert saved.value.ok
        item = saved.value.json()["item"]
        # Legacy public/published fields mean saved to the authenticated gallery.
        assert item["public_status"] == "published" and item["visibility"] == "public"
        item_id = item["id"]
        share = page.evaluate("""async (id) => {
            const response = await fetch(`/api/items/${id}/share`);
            return {status: response.status, body: await response.json()};
        }""", item_id)
        assert share["status"] == 200
        assert share["body"]["enabled"] is False and share["body"]["active"] is False
        assert share["body"]["url"] is None and share["body"]["media_url"] is None
        page.get_by_role("navigation").get_by_role("button", name="Gallery", exact=True).click()
        page.locator(f'.itemCard[data-item-id="{item_id}"]').click()
        page.get_by_role("button", name="Edit details", exact=True).click()
        editor = page.get_by_role("dialog", name="Edit meme", exact=True)
        description = editor.get_by_role("textbox", name="Description", exact=True)
        description.fill("guideuniqueviolet")
        editor.get_by_role("button", name="Save changes", exact=True).click()
        expect(editor.get_by_role("status")).to_have_text("Saved")
        editor.get_by_role("button", name="Back", exact=True).click()

        def search(term, expected_ids):
            with page.expect_response(lambda response: response.request.method == "GET"
                    and urlsplit(response.url).path == "/api/items"
                    and parse_qs(urlsplit(response.url).query).get("q") == [term]) as result:
                page.get_by_placeholder("Search archive").fill(term)
            assert result.value.ok
            assert [row["id"] for row in result.value.json()["items"]] == expected_ids
            expect(page.locator(".itemCard")).to_have_count(len(expected_ids))
            for expected_id in expected_ids:
                expect(page.locator(f'.itemCard[data-item-id="{expected_id}"]')).to_be_visible()

        search("guidenomatch", [])
        search("guideuniqueviolet", [item_id])
        page.reload(wait_until="networkidle")
        search("guidenomatch", [])
        search("guideuniqueviolet", [item_id])
        page.locator(f'.itemCard[data-item-id="{item_id}"]').click()
        page.get_by_role("button", name="Edit details", exact=True).click()
        expect(description).to_have_value("guideuniqueviolet")
        assert not unexpected, unexpected
        print("Installed private HTTPS UI upload/save/gallery/metadata/search/reload passed")
        print("CI browser:", browser.version)
    finally:
        browser.close()
