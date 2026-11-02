from unittest.mock import Mock

import pytest

from test_dashboard_chromium import BrowserPage


@pytest.mark.parametrize("message", ["Cannot find default execution context", "Execution context was destroyed."])
def test_selector_wait_reconnects_after_navigation(message):
    page = BrowserPage(process=Mock(), cdp=Mock())
    page.cdp.send.side_effect = [AssertionError({"code": -32000, "message": message}), {"result": {"value": True}}]

    page.wait_for_selector(".itemCard")

    assert page.cdp.send.call_count == 2


def test_selector_wait_does_not_hide_script_failures():
    page = BrowserPage(process=Mock(), cdp=Mock())
    page.cdp.send.side_effect = AssertionError("Real script failure")

    with pytest.raises(AssertionError, match="Real script failure"):
        page.wait_for_selector(".itemCard")

    assert page.cdp.send.call_count == 1
