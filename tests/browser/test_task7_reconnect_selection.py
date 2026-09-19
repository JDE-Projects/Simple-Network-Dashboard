"""Task 7 coverage: a tab resends its device selection after a reconnect.

Device selection lives per WebSocket connection on the server, so a socket that
drops and automatically reconnects starts with no selection and would silently
stop receiving that tab's metrics. The browser guards against this by resending
its current selection from the socket's ``onopen``. This drives a real drop and
reconnect in Chromium and confirms the ``select_device`` frame is sent again.
"""

from __future__ import annotations

import json
import re
import time

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import DASHBOARD_PASSWORD, SEEDED_DEVICE_ID

# Record every frame the page sends on any WebSocket, before app scripts run.
# add_init_script runs this source on each new document, so it must be the body
# to execute, not an arrow function that is defined and never called.
_RECORDER = """
window.__sent = [];
const _origSend = WebSocket.prototype.send;
WebSocket.prototype.send = function (data) {
  window.__sent.push(data);
  return _origSend.call(this, data);
};
"""


def _selected_for(sent, device_id):
    """True if any recorded frame selects the given device."""
    for raw in sent:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if msg.get("type") == "select_device" and msg.get("id") == device_id:
            return True
    return False


def _wait_until(page, predicate, *, timeout=15.0):
    """Pump the browser event loop until predicate() is true or time runs out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        page.wait_for_timeout(200)
    return False


@pytest.mark.browser
def test_selection_resent_after_reconnect(_chromium_available, page, app_server_with_device):
    base_url = app_server_with_device
    page.add_init_script(_RECORDER)

    page.goto(f"{base_url}/login")
    page.fill("#password", DASHBOARD_PASSWORD)
    page.click("#submit")
    page.wait_for_url(f"{base_url}/")
    expect(page.locator("#verLabel")).to_have_text(re.compile(r"^v"))

    # Select the device: this sends the first select_device on the live socket.
    page.click(f'[data-card="{SEEDED_DEVICE_ID}"]')
    assert _wait_until(
        page, lambda: _selected_for(page.evaluate("() => window.__sent"), SEEDED_DEVICE_ID)
    ), "the initial selection was never sent"

    # Clear the record, then drop the socket. The app reconnects on its own
    # timer, and only a resend from the new socket's onopen can refill this.
    page.evaluate("() => { window.__sent = []; }")
    page.evaluate("() => ws.close()")

    assert _wait_until(
        page, lambda: _selected_for(page.evaluate("() => window.__sent"), SEEDED_DEVICE_ID)
    ), "the tab did not resend its selection after reconnecting"

    # The socket is healthy again (the chip goes back to its connected state).
    expect(page.locator("#wsChip")).to_have_class(re.compile(r"\bok\b"))
