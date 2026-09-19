"""Task 10 coverage: the Add/Edit Device modal recovers from API failures.

See static/index.html's #devSave handler (~line 1755): it disables the
button and shows 'Saving…', shows an inline error in #devErr on failure
without closing the modal, and restores the button label in a `finally`.
This does not need a pre-existing device: it drives the Add Device flow.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

NAME = "Recovery Test Device"
HOST = "10.0.0.201"
USER = "recoveryuser"


def _open_and_fill(page):
    page.click("#addBtn")
    page.fill("#fName", NAME)
    page.fill("#fHost", HOST)
    page.fill("#fUser", USER)


def _assert_modal_recovered(page):
    save_btn = page.locator("#devSave")
    expect(page.locator("#deviceModal")).to_have_class(re.compile(r"\bshow\b"))
    expect(save_btn).to_be_enabled()
    expect(save_btn).to_have_text("Save Device")
    expect(page.locator("#fName")).to_have_value(NAME)
    expect(page.locator("#fHost")).to_have_value(HOST)
    expect(page.locator("#fUser")).to_have_value(USER)


def test_device_save_recovers_from_dropped_network(logged_in_page, app_server):
    page = logged_in_page
    page.route("**/api/devices", lambda route: route.abort() if route.request.method == "POST" else route.continue_())
    _open_and_fill(page)
    page.click("#devSave")
    _assert_modal_recovered(page)
    expect(page.locator("#devErr")).to_contain_text("Could not reach the server")


def test_device_save_recovers_from_server_error(logged_in_page, app_server):
    page = logged_in_page
    page.route(
        "**/api/devices",
        lambda route: route.fulfill(status=500, content_type="text/plain", body="Internal Server Error")
        if route.request.method == "POST"
        else route.continue_(),
    )
    _open_and_fill(page)
    page.click("#devSave")
    _assert_modal_recovered(page)
    err = page.locator("#devErr")
    expect(err).to_contain_text("The server hit an error")
    expect(err).not_to_contain_text("Internal Server Error")


def test_device_save_recovers_from_expired_session(logged_in_page, app_server):
    page = logged_in_page
    page.route(
        "**/api/devices",
        lambda route: route.fulfill(status=401, content_type="application/json", body='{"detail":"unauthorized"}')
        if route.request.method == "POST"
        else route.continue_(),
    )
    _open_and_fill(page)
    page.click("#devSave")
    _assert_modal_recovered(page)
    expect(page.locator("#devErr")).to_contain_text("Your session has expired")
    expect(page.locator("#sessionExpiredModal")).to_have_class(re.compile(r"\bshow\b"))


@pytest.mark.xfail(
    reason=(
        "known pre-existing gap outside Task 10 scope: a 200 response with an "
        "unparsable body crashes #devSave's success path (DEVICES = undefined). "
        "See this test's docstring; fixing static/index.html is out of scope here."
    ),
    strict=False,
)
def test_device_save_survives_malformed_response(logged_in_page, app_server):
    """Documents a real gap, not something this task is allowed to fix.

    A 200 response whose body is not valid JSON is treated by api() as a bare
    success ({ok: true}) with no `devices` field. #devSave's success branch
    then runs `DEVICES = r.devices` (undefined) and calls renderDevices(),
    which throws on `DEVICES.length`. That is a real front-end crash on this
    specific input, unrelated to Task 10's network/500/timeout/401 recovery
    work, and editing static/index.html is out of scope for this change. This
    test is expected to fail so the gap is visible rather than silently
    skipped.
    """
    page = logged_in_page
    page.route(
        "**/api/devices",
        lambda route: route.fulfill(status=200, content_type="application/json", body="not json{")
        if route.request.method == "POST"
        else route.continue_(),
    )
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    _open_and_fill(page)
    page.click("#devSave")
    page.wait_for_timeout(300)
    assert not errors, f"known gap: malformed 200 crashes devSave's success path: {errors}"
