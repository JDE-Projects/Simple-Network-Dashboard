"""Running-command controls survive redraw, reload, and reconnect."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import DASHBOARD_PASSWORD, SEEDED_DEVICE_ID


@pytest.mark.browser
def test_running_command_state_survives_redraw_reload_and_reconnect(page, app_server_with_device):
    running = {"value": True}
    connected = {"value": True}
    routes = []

    def handle_ws(ws) -> None:
        server = ws.connect_to_server()
        ws.on_message(lambda message: server.send(message))

        def forward(message) -> None:
            msg = json.loads(message)
            if msg.get("type") == "init":
                msg["ssh_connected"] = [SEEDED_DEVICE_ID] if connected["value"] else []
                msg["ssh_running"] = [SEEDED_DEVICE_ID] if running["value"] else []
                message = json.dumps(msg)
            ws.send(message)

        server.on_message(forward)
        routes.append(ws)

    page.route_web_socket(re.compile(r"/ws"), handle_ws)
    page.goto(f"{app_server_with_device}/login")
    page.fill("#password", DASHBOARD_PASSWORD)
    page.click("#submit")
    page.wait_for_url(f"{app_server_with_device}/")

    card = page.locator(f'[data-card="{SEEDED_DEVICE_ID}"]')
    stop = card.locator("[data-cancel]")
    expect(stop).to_be_visible()
    expect(card.locator("[data-run]")).to_be_hidden()
    expect(card.locator("[data-custom]")).to_be_disabled()
    expect(card.locator("[data-disconnect]")).to_be_disabled()
    expect(page.locator(f'.tab[data-tab="{SEEDED_DEVICE_ID}"]')).to_have_class(re.compile(r"\brunning\b"))

    routes[-1].send(json.dumps({"type": "devices", "devices": [{
        "id": SEEDED_DEVICE_ID, "name": "Seeded Test Device", "host": "10.0.0.99",
        "username": "tester", "metrics_port": 9100, "commands": [],
    }]}))
    expect(stop).to_be_visible()
    expect(card.locator("[data-run]")).to_be_hidden()

    page.reload()
    expect(stop).to_be_visible()
    expect(card.locator("[data-run]")).to_be_hidden()

    with page.expect_request("**/api/ssh/cancel") as request_info:
        stop.click()
    assert request_info.value.post_data_json == {"device_id": SEEDED_DEVICE_ID}

    running["value"] = False
    page.evaluate("() => ws.close()")
    expect(card.locator("[data-run]")).to_be_visible(timeout=8_000)
    expect(stop).to_be_hidden()
    expect(page.locator(f'.tab[data-tab="{SEEDED_DEVICE_ID}"]')).to_have_class(re.compile(r"\bconnected\b"))

    connected["value"] = False
    page.evaluate("() => ws.close()")
    expect(card).not_to_have_class(re.compile(r"\bssh-on\b"), timeout=8_000)
    expect(card.locator("[data-run]")).to_be_hidden()
    expect(stop).to_be_hidden()
    expect(page.locator(f'.tab[data-tab="{SEEDED_DEVICE_ID}"]')).to_have_class(re.compile(r"\bdisconnected\b"))
