"""Task 12 coverage: concealed library and custom command console lines."""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import (
    SEEDED_DEVICE_HOST,
    SEEDED_DEVICE_ID,
    SEEDED_DEVICE_NAME,
)

LIBRARY_NAME = "Test library command"
LIBRARY_COMMAND = "echo library-command-secret"
CUSTOM_COMMAND = "echo custom-command-secret"


@pytest.mark.browser
def test_library_and_custom_commands_conceal_reveal_and_export(connected_device_page):
    page = connected_device_page

    def run_route(route):
        route.fulfill(status=200, content_type="application/json", body='{"ok": true}')

    page.route("**/api/ssh/run", run_route)

    # The app runs on Windows in this harness, where a genuine disk save trips on
    # POSIX-only calls (directory fsync, fchmod). Fulfill the command-save PUT with
    # a valid success so the modal closes; this test is about console concealment,
    # not persistence, which the Linux install and pytest suite cover.
    def commands_route(route):
        body = json.loads(route.request.post_data)
        device = {
            "id": SEEDED_DEVICE_ID,
            "name": SEEDED_DEVICE_NAME,
            "host": SEEDED_DEVICE_HOST,
            "username": "tester",
            "metrics_port": 9100,
            "commands": body["commands"],
        }
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "devices": [device]}),
        )

    page.route(f"**/api/devices/{SEEDED_DEVICE_ID}/commands", commands_route)

    page.click(f'[data-card="{SEEDED_DEVICE_ID}"]')  # select the device so #addCmdBtn enables
    page.click("#addCmdBtn")
    page.fill("#cName", LIBRARY_NAME)
    page.fill("#cCmd", LIBRARY_COMMAND)
    page.click("#cmdSave")
    library_card = page.locator(".cmd-card", has_text=LIBRARY_NAME)
    expect(library_card).to_be_visible()
    with page.expect_request("**/api/ssh/run") as library_req:
        library_card.locator(".run").click()
    library_request = json.loads(library_req.value.post_data)
    assert library_request["command"] == LIBRARY_COMMAND
    assert library_request["label"] == LIBRARY_NAME
    assert library_request["cmd_id"]
    page.evaluate(
        """message => ws.onmessage({data: JSON.stringify(message)})""",
        {
            "type": "ssh_log",
            "device_id": SEEDED_DEVICE_ID,
            "text": f"$ {LIBRARY_NAME}",
            "level": "cmd",
            "cmd_id": library_request["cmd_id"],
        },
    )

    library_line = page.locator('[data-console="dev_browsertest01"] .ln.custom-cmd')
    library_msg = library_line.locator(".msg")
    expect(library_msg).to_have_text("$ Library Command (click to reveal)")
    expect(library_line).not_to_contain_text(LIBRARY_NAME)
    expect(library_line).not_to_contain_text(LIBRARY_COMMAND)
    reveal = library_line.locator(".reveal-toggle")
    expect(reveal).to_have_attribute("aria-expanded", "false")
    expect(reveal).to_have_attribute("aria-label", "Reveal command")
    reveal.focus()
    page.keyboard.press("Enter")
    expect(library_line).to_contain_text(LIBRARY_COMMAND)
    expect(reveal).to_have_attribute("aria-expanded", "true")
    expect(reveal).to_have_attribute("aria-label", "Hide command")
    page.keyboard.press("Space")
    expect(library_msg).to_have_text("$ Library Command (click to reveal)")

    with page.expect_download() as hidden_download:
        page.click("#exportBtn")
        page.click("#exportHide")
    hidden_export = hidden_download.value.path().read_text(encoding="utf-8")
    assert "$ Library Command" in hidden_export
    assert LIBRARY_COMMAND not in hidden_export

    page.fill(f'[data-card="{SEEDED_DEVICE_ID}"] [data-custom]', CUSTOM_COMMAND)
    with page.expect_request("**/api/ssh/run") as custom_req:
        page.click(f'[data-card="{SEEDED_DEVICE_ID}"] [data-run]')
    custom_request = json.loads(custom_req.value.post_data)
    page.evaluate(
        """message => ws.onmessage({data: JSON.stringify(message)})""",
        {
            "type": "ssh_log",
            "device_id": SEEDED_DEVICE_ID,
            "text": "$ Custom command",
            "level": "cmd",
            "cmd_id": custom_request["cmd_id"],
        },
    )
    # Running a custom command offers to save it; dismiss so it stops covering Export.
    page.click("#saveSkip")

    custom_line = page.locator('[data-console="dev_browsertest01"] .ln.custom-cmd').nth(1)
    expect(custom_line.locator(".msg")).to_have_text("$ Custom Command (click to reveal)")

    with page.expect_download() as hidden_custom_download:
        page.click("#exportBtn")
        page.click("#exportHide")
    hidden_custom_export = hidden_custom_download.value.path().read_text(encoding="utf-8")
    assert "$ Custom Command" in hidden_custom_export
    assert CUSTOM_COMMAND not in hidden_custom_export

    with page.expect_download() as shown_download:
        page.click("#exportBtn")
        page.click("#exportShow")
    shown_export = shown_download.value.path().read_text(encoding="utf-8")
    assert LIBRARY_COMMAND in shown_export
    assert CUSTOM_COMMAND in shown_export
