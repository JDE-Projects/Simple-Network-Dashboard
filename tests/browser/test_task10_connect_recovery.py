"""Task 10 coverage: the Connect button on a device card recovers from every
kind of API failure instead of getting stuck disabled or on 'Connecting…'.

See static/index.html's tryConnect() (~line 1450), which resets the button in
a `finally` block, and api()/apiFailure() (~line 874) for how each failure
kind is turned into a message.
"""

from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.browser.conftest import SEEDED_DEVICE_ID


def _card(page):
    return page.locator(f'[data-card="{SEEDED_DEVICE_ID}"]')


def _try_connect(page):
    card = _card(page)
    card.locator("[data-pw]").fill("some-password-123")
    card.locator("[data-connect]").click()
    return card


def test_connect_recovers_from_dropped_network(logged_in_page_with_device, app_server_with_device):
    page = logged_in_page_with_device
    page.route("**/api/ssh/connect", lambda route: route.abort())
    card = _try_connect(page)
    connect_btn = card.locator("[data-connect]")
    expect(connect_btn).to_be_enabled()
    expect(connect_btn).to_have_text("Connect")
    expect(card.locator("[data-cerr]")).to_contain_text("Could not reach the server")


def test_connect_recovers_from_server_error(logged_in_page_with_device, app_server_with_device):
    page = logged_in_page_with_device
    # A plain-text 500 body (e.g. a proxy error page), not a JSON one: the
    # app's api() only forwards a response body verbatim when it parses as a
    # JSON object, so this is what proves a raw server error never reaches
    # the screen.
    page.route(
        "**/api/ssh/connect",
        lambda route: route.fulfill(status=500, content_type="text/plain", body="Internal Server Error"),
    )
    card = _try_connect(page)
    connect_btn = card.locator("[data-connect]")
    expect(connect_btn).to_be_enabled()
    expect(connect_btn).to_have_text("Connect")
    err = card.locator("[data-cerr]")
    expect(err).to_contain_text("The server hit an error")
    expect(err).not_to_contain_text("Internal Server Error")


def test_connect_shows_error_on_expired_session(logged_in_page_with_device, app_server_with_device):
    page = logged_in_page_with_device
    page.route(
        "**/api/ssh/connect",
        lambda route: route.fulfill(status=401, content_type="application/json", body='{"detail":"unauthorized"}'),
    )
    card = _try_connect(page)
    connect_btn = card.locator("[data-connect]")
    expect(connect_btn).to_be_enabled()
    expect(connect_btn).to_have_text("Connect")
    expect(card.locator("[data-cerr]")).to_contain_text("Your session has expired")
    expect(page.locator("#sessionExpiredModal")).to_have_class(re.compile(r"\bshow\b"))


def test_connect_survives_malformed_response(logged_in_page_with_device, app_server_with_device):
    # A 200 with a body that isn't valid JSON is treated by api() as a bare
    # success ({ok: true}), the same way a normal empty-body 200 would be.
    # tryConnect() then takes its success branch: it does not crash and does
    # not render any of the raw "not json{" body.
    page = logged_in_page_with_device
    page.route(
        "**/api/ssh/connect",
        lambda route: route.fulfill(status=200, content_type="application/json", body="not json{"),
    )
    card = _try_connect(page)
    connect_btn = card.locator("[data-connect]")
    expect(connect_btn).to_be_enabled()
    expect(connect_btn).to_have_text("Connect")
    expect(card.locator("[data-cerr]")).not_to_contain_text("not json")
    expect(page.locator("body")).not_to_contain_text("undefined")


def test_connect_button_stays_busy_while_request_hangs(logged_in_page_with_device, app_server_with_device):
    """A request that never answers must leave the button in its busy state
    (not crash, not silently reset) until the client-side timeout fires.

    This checks the busy state cheaply, without waiting the real 20 seconds;
    see test_connect_recovers_after_real_20s_timeout for the full wait.
    """
    page = logged_in_page_with_device
    page.route("**/api/ssh/connect", lambda route: None)  # never resolve
    card = _try_connect(page)
    connect_btn = card.locator("[data-connect]")
    expect(connect_btn).to_be_disabled()
    expect(connect_btn).to_have_text("Connecting…")


def test_connect_recovers_after_real_20s_timeout(logged_in_page_with_device, app_server_with_device):
    """Waits out the real, hardcoded 20-second client-side timeout in api().

    The task explicitly does not want a literal 20s sleep faked away: this is
    the one test in this file that pays the real cost, so the rest of the
    connect-recovery coverage above can stay fast and check the busy state
    instead. Expect timeouts below are extended past 20s for that reason.
    """
    page = logged_in_page_with_device
    page.route("**/api/ssh/connect", lambda route: None)  # never resolve
    card = _try_connect(page)
    connect_btn = card.locator("[data-connect]")
    expect(connect_btn).to_be_enabled(timeout=25_000)
    expect(connect_btn).to_have_text("Connect")
    expect(card.locator("[data-cerr]")).to_contain_text(
        "The server took too long to respond", timeout=25_000
    )
