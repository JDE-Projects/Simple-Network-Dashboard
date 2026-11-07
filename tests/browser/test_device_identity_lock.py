"""The device editor locks host and username while the device has an SSH session.

The seeded device's host is not a real SSH server, so a connected device is
simulated the way conftest.connected_device_page does it: the real WebSocket
traffic passes through to the server and extra 'ssh_status' frames are
injected on the client side. This fixture keeps the socket handle so a test
can also inject the later disconnect.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import DASHBOARD_PASSWORD, SEEDED_DEVICE_ID

NOTE = "Disconnect to change host or username."
REFUSAL = "Disconnect this device before changing its host or username."


@pytest.fixture
def connected_page_with_socket(_chromium_available, page, app_server_with_device: str):
    """A logged-in page whose seeded device looks connected, plus a function
    that injects a server message into that page."""
    ws_routes: list = []

    def handle_ws(ws) -> None:
        server = ws.connect_to_server()
        ws.on_message(lambda message: server.send(message))
        server.on_message(lambda message: ws.send(message))
        ws_routes.append(ws)

    page.route_web_socket(re.compile(r"/ws"), handle_ws)
    page.goto(f"{app_server_with_device}/login")
    page.fill("#password", DASHBOARD_PASSWORD)
    page.click("#submit")
    page.wait_for_url(f"{app_server_with_device}/")
    expect(page.locator("#verLabel")).to_have_text(re.compile(r"^v"))
    card = page.locator(f'[data-card="{SEEDED_DEVICE_ID}"]')
    expect(card).to_be_visible()
    assert ws_routes, "the dashboard's WebSocket never connected"

    def inject(message: dict) -> None:
        ws_routes[0].send(json.dumps(message))

    inject({"type": "ssh_status", "device_id": SEEDED_DEVICE_ID, "state": "connected"})
    expect(card).to_have_class(re.compile(r"\bssh-on\b"))
    return page, inject


def _open_editor(page) -> None:
    page.click(f'[data-card="{SEEDED_DEVICE_ID}"] [data-edit]')
    expect(page.locator("#deviceModal")).to_have_class(re.compile(r"\bshow\b"))


def test_editor_locks_host_and_username_for_a_connected_device(connected_page_with_socket):
    page, _inject = connected_page_with_socket
    _open_editor(page)

    expect(page.locator("#fHost")).to_be_disabled()
    expect(page.locator("#fUser")).to_be_disabled()
    expect(page.locator("#fIdentityNote")).to_be_visible()
    expect(page.locator("#fIdentityNote")).to_have_text(NOTE)
    expect(page.locator("#fName")).to_be_enabled()
    expect(page.locator("#fPort")).to_be_enabled()


def test_editor_unlocks_when_the_device_disconnects_and_keeps_typed_values(connected_page_with_socket):
    page, inject = connected_page_with_socket
    _open_editor(page)
    page.fill("#fName", "Typed while connected")
    host_before = page.locator("#fHost").input_value()
    expect(page.locator("#fHost")).to_be_disabled()

    inject({"type": "ssh_status", "device_id": SEEDED_DEVICE_ID, "state": "idle"})

    expect(page.locator("#fHost")).to_be_enabled()
    expect(page.locator("#fUser")).to_be_enabled()
    expect(page.locator("#fIdentityNote")).to_be_hidden()
    expect(page.locator("#fName")).to_have_value("Typed while connected")
    expect(page.locator("#fHost")).to_have_value(host_before)


def test_refused_save_shows_the_server_message_and_keeps_the_dialog_open(connected_page_with_socket):
    page, _inject = connected_page_with_socket
    page.route(
        "**/api/devices",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": False, "error": REFUSAL}),
        )
        if route.request.method == "POST"
        else route.continue_(),
    )
    _open_editor(page)
    page.fill("#fName", "Renamed")
    page.click("#devSave")

    expect(page.locator("#devErr")).to_have_text(REFUSAL)
    expect(page.locator("#deviceModal")).to_have_class(re.compile(r"\bshow\b"))
    expect(page.locator("#devSave")).to_be_enabled()
