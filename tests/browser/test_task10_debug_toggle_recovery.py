"""Task 10 coverage: the server debug-log toggle reverts on API failures.

See static/index.html's #debugChk change handler (~line 1920): on a failed
POST /api/debug it flips the checkbox back to its prior state and raises a
red error banner. The checkbox is visually a switch (a styled <span> sits on
top of it), so tests click the switch track rather than the hidden input.
"""

from __future__ import annotations

import re

from playwright.sync_api import expect


def _toggle(page):
    was_checked = page.is_checked("#debugChk")
    page.click(".dbg-track")
    return was_checked


def test_debug_toggle_reverts_on_dropped_network(logged_in_page, app_server):
    page = logged_in_page
    page.route("**/api/debug", lambda route: route.abort())
    was_checked = _toggle(page)
    expect(page.locator("#debugChk")).to_have_js_property("checked", was_checked)
    expect(page.locator(".error-banner .eb-text")).to_contain_text(
        "Could not change debug logging"
    )


def test_debug_toggle_reverts_on_server_error(logged_in_page, app_server):
    page = logged_in_page
    page.route(
        "**/api/debug",
        lambda route: route.fulfill(status=500, content_type="text/plain", body="Internal Server Error"),
    )
    was_checked = _toggle(page)
    expect(page.locator("#debugChk")).to_have_js_property("checked", was_checked)
    banner = page.locator(".error-banner .eb-text")
    expect(banner).to_contain_text("Could not change debug logging")
    expect(banner).not_to_contain_text("Internal Server Error")


def test_debug_toggle_reverts_on_expired_session(logged_in_page, app_server):
    page = logged_in_page
    page.route(
        "**/api/debug",
        lambda route: route.fulfill(status=401, content_type="application/json", body='{"detail":"unauthorized"}'),
    )
    was_checked = _toggle(page)
    expect(page.locator("#debugChk")).to_have_js_property("checked", was_checked)
    expect(page.locator("#sessionExpiredModal")).to_have_class(re.compile(r"\bshow\b"))


def test_debug_toggle_survives_malformed_response(logged_in_page, app_server):
    # A 200 with an unparsable body is treated as bare success by api(), so
    # the checkbox is left in its new (checked) state and no banner appears.
    # This just checks that path does not crash or leak the raw body.
    page = logged_in_page
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.route(
        "**/api/debug",
        lambda route: route.fulfill(status=200, content_type="application/json", body="not json{"),
    )
    _toggle(page)
    page.wait_for_timeout(300)
    assert not errors
    expect(page.locator("body")).not_to_contain_text("not json")
