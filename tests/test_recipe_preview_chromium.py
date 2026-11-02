"""The embedded recipe must not expose a second scrolling canvas."""

from io import BytesIO

from PIL import Image
import pytest

from meme_arsenal.remix_page import render_remix_page
# Re-export both fixtures so pytest can resolve workflow's workflow_url dependency.
from test_dashboard_workflow import workflow as workflow, workflow_url as workflow_url


pytestmark = pytest.mark.chromium


def test_preview_has_one_scroll_surface_and_an_opaque_bottom(workflow):
    page, state, width = workflow
    state["items"][1]["origin"] = "remix"
    state["share_active"] = True  # Output publication must not imply recipe consent.
    description = "A longer recipe description that remains readable while scrolling. " * 12
    response = render_remix_page(
        image_url="/fixture/79.png",
        sources=[(description, "/fixture/79.png"), (description, "/fixture/78.png")],
        summary=description,
        tweaks={"summary": description},
    )
    page.route("**/api/items/79/remix-card?page=true", lambda route: route.fulfill(
        body=response.body, headers=dict(response.headers),
    ))
    page.reload()
    page.locator('.itemCard[data-item-id="79"]').click()
    page.get_by_role("button", name="Remix Page", exact=True).click()
    playwright = pytest.importorskip("playwright.sync_api")
    playwright.expect(page.get_by_role("button", name="Publish page", exact=True)).to_be_enabled()
    frame = page.locator(".recipePreview")
    page.frame_locator(".recipePreview").locator(".flags").wait_for()
    page.wait_for_function("""() => {
        const frame = document.querySelector('.recipePreview');
        return frame.contentDocument.body.scrollHeight <= frame.clientHeight + 1;
    }""")
    assert frame.evaluate("""frame => {
        const doc = frame.contentDocument;
        return getComputedStyle(doc.documentElement).overflow === 'hidden'
            && getComputedStyle(doc.body).overflow === 'hidden'
            && getComputedStyle(doc.body).backgroundColor === getComputedStyle(frame).backgroundColor;
    }""")
    panel = page.locator(".fullPage")
    assert panel.locator("a[download]").count() == 0
    assert panel.evaluate("e => getComputedStyle(e).overscrollBehaviorY") == "none"
    for height in (740, 900):
        page.set_viewport_size({"width": width, "height": height})
        panel.evaluate("e => e.scrollTop = e.scrollHeight")
        page.wait_for_timeout(100)
        assert panel.bounding_box()["height"] == height
        screenshot = Image.open(BytesIO(page.screenshot())).convert("RGB")
        assert screenshot.getpixel((width // 2, height - 4)) == (255, 245, 252)
    assert state["share_count"] == state["save_count"] == 0
