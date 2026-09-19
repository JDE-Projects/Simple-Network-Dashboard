"""Task 9 coverage: the browser warning banner when debug logging fails.

The server emits a ``debug_failed`` message (and an ``init`` flag for tabs that
connect after a failure) when the debug log can no longer be written; that
server side is covered by ``tests/test_debug_log_failure.py``. This test covers
the browser half: the tab must show a clear, dismissable banner and clear the
debug checkbox so the user is never left believing logging is still on.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

DEBUG_FAILED_TEXT = (
    "Debug logging stopped because the log file could not be written. "
    "Check the server log directory."
)


@pytest.mark.browser
def test_debug_failed_message_shows_banner_and_clears_checkbox(logged_in_page):
    page = logged_in_page

    # Turn debug logging on first so the flip back to off is meaningful. The
    # harness log directory is writable, so this real toggle succeeds and sticks.
    # The visible toggle track sits over the real checkbox, so click by force.
    page.locator("#debugChk").check(force=True)
    expect(page.locator("#debugChk")).to_be_checked()

    banner = page.locator("#errorBanners .error-banner .eb-text", has_text=DEBUG_FAILED_TEXT)
    expect(banner).to_have_count(0)

    # Deliver the server's real failure message through the live socket handler.
    page.evaluate(
        """message => ws.onmessage({data: JSON.stringify(message)})""",
        {"type": "debug_failed"},
    )

    expect(banner).to_be_visible()
    expect(page.locator("#debugChk")).not_to_be_checked()

    # A repeat of the same message must not stack a second identical banner.
    page.evaluate(
        """message => ws.onmessage({data: JSON.stringify(message)})""",
        {"type": "debug_failed"},
    )
    expect(banner).to_have_count(1)

    # The banner is dismissable.
    page.locator("#errorBanners .error-banner", has_text=DEBUG_FAILED_TEXT).locator(".eb-close").click()
    expect(banner).to_have_count(0)
