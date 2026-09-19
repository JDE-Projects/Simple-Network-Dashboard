"""Task 10 coverage: the 'Check for updates' button recovers from API
failures instead of getting stuck disabled or on 'Checking…'.

See static/index.html's #updateBtn handler (~line 1939): it disables the
button and shows 'Checking…', shows a notice plus an error modal on
failure, and restores the button label in a `finally`.
"""

from __future__ import annotations

import re

from playwright.sync_api import expect


def _check_for_updates(page):
    page.click("#updateBtn")


def _assert_button_recovered(page):
    btn = page.locator("#updateBtn")
    expect(btn).to_be_enabled()
    expect(btn).to_have_text("Check for updates")


def test_update_check_recovers_from_dropped_network(logged_in_page, app_server):
    page = logged_in_page
    page.route("**/api/check-update", lambda route: route.abort())
    _check_for_updates(page)
    _assert_button_recovered(page)
    expect(page.locator("#updateNotice")).to_contain_text("Couldn't reach GitHub")


def test_update_check_recovers_from_server_error(logged_in_page, app_server):
    page = logged_in_page
    page.route(
        "**/api/check-update",
        lambda route: route.fulfill(status=500, content_type="text/plain", body="Internal Server Error"),
    )
    _check_for_updates(page)
    _assert_button_recovered(page)
    notice = page.locator("#updateNotice")
    expect(notice).to_contain_text("Couldn't reach GitHub")
    expect(notice).not_to_contain_text("Internal Server Error")


def test_update_check_recovers_from_expired_session(logged_in_page, app_server):
    page = logged_in_page
    page.route(
        "**/api/check-update",
        lambda route: route.fulfill(status=401, content_type="application/json", body='{"detail":"unauthorized"}'),
    )
    _check_for_updates(page)
    _assert_button_recovered(page)
    expect(page.locator("#sessionExpiredModal")).to_have_class(re.compile(r"\bshow\b"))


def test_update_check_recovers_from_malformed_response(logged_in_page, app_server):
    # A 200 with an unparsable body is treated as a failure by api(), so the
    # button takes its failure branch and shows the "couldn't reach GitHub"
    # notice, rather than "You're on the latest version (vundefined)".
    page = logged_in_page
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.route(
        "**/api/check-update",
        lambda route: route.fulfill(status=200, content_type="application/json", body="not json{"),
    )
    _check_for_updates(page)
    _assert_button_recovered(page)
    notice = page.locator("#updateNotice")
    expect(notice).to_contain_text("Couldn't reach GitHub")
    expect(notice).not_to_contain_text("vundefined")
    expect(notice).not_to_contain_text("not json")
    assert not errors
