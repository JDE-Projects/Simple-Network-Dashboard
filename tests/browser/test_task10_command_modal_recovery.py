"""Task 10 coverage: the Add/Edit Command modal recovers from API failures.

See static/index.html's persistCommands() (~line 1635) and #cmdSave handler
(~line 1682): it disables the button and shows 'Saving…', shows an inline
error in #cmdErr on failure without closing the modal, rolls the optimistic
local edit back from the server, and restores the button label in a
`finally`. Needs an existing device (selecting it makes #addCmdBtn work).
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import SEEDED_DEVICE_ID

NAME = "Recovery Test Command"
COMMAND = "echo recovery-test"


def _open_and_fill(page):
    page.click(f'[data-card="{SEEDED_DEVICE_ID}"]')  # select the device
    page.click("#addCmdBtn")
    page.fill("#cName", NAME)
    page.fill("#cCmd", COMMAND)


def _assert_modal_recovered(page):
    save_btn = page.locator("#cmdSave")
    expect(page.locator("#cmdModal")).to_have_class(re.compile(r"\bshow\b"))
    expect(save_btn).to_be_enabled()
    expect(save_btn).to_have_text("Save Command")
    expect(page.locator("#cName")).to_have_value(NAME)
    expect(page.locator("#cCmd")).to_have_value(COMMAND)


def _commands_route(handler):
    return lambda route: handler(route) if route.request.method == "PUT" else route.continue_()


def test_command_save_recovers_from_dropped_network(logged_in_page_with_device, app_server_with_device):
    page = logged_in_page_with_device
    page.route(
        f"**/api/devices/{SEEDED_DEVICE_ID}/commands",
        _commands_route(lambda route: route.abort()),
    )
    _open_and_fill(page)
    page.click("#cmdSave")
    _assert_modal_recovered(page)
    expect(page.locator("#cmdErr")).to_contain_text("Save failed")


def test_command_save_recovers_from_server_error(logged_in_page_with_device, app_server_with_device):
    page = logged_in_page_with_device
    page.route(
        f"**/api/devices/{SEEDED_DEVICE_ID}/commands",
        _commands_route(
            lambda route: route.fulfill(status=500, content_type="text/plain", body="Internal Server Error")
        ),
    )
    _open_and_fill(page)
    page.click("#cmdSave")
    _assert_modal_recovered(page)
    err = page.locator("#cmdErr")
    expect(err).to_contain_text("Save failed")
    expect(err).not_to_contain_text("Internal Server Error")


def test_command_save_recovers_from_expired_session(logged_in_page_with_device, app_server_with_device):
    page = logged_in_page_with_device
    page.route(
        f"**/api/devices/{SEEDED_DEVICE_ID}/commands",
        _commands_route(
            lambda route: route.fulfill(status=401, content_type="application/json", body='{"detail":"unauthorized"}')
        ),
    )
    _open_and_fill(page)
    page.click("#cmdSave")
    _assert_modal_recovered(page)
    expect(page.locator("#sessionExpiredModal")).to_have_class(re.compile(r"\bshow\b"))


@pytest.mark.xfail(
    reason=(
        "known pre-existing gap outside Task 10 scope: a 200 response with an "
        "unparsable body crashes persistCommands()'s success path (DEVICES = "
        "undefined). Fixing static/index.html is out of scope here."
    ),
    strict=False,
)
def test_command_save_survives_malformed_response(logged_in_page_with_device, app_server_with_device):
    page = logged_in_page_with_device
    page.route(
        f"**/api/devices/{SEEDED_DEVICE_ID}/commands",
        _commands_route(
            lambda route: route.fulfill(status=200, content_type="application/json", body="not json{")
        ),
    )
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    _open_and_fill(page)
    page.click("#cmdSave")
    page.wait_for_timeout(300)
    assert not errors, f"known gap: malformed 200 crashes persistCommands' success path: {errors}"
