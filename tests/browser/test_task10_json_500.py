"""Regression coverage: a JSON 500 body in FastAPI's generic error shape
({"detail": "Internal server error."}, from main.py's global 500 handler)
must not be forwarded verbatim to the caller. api() (static/index.html,
~line 886) only treats a parsed non-2xx object as the app's own error shape
when it has a string `error` field; anything else falls through to the
friendly "server hit an error" message, matching the plain-text 500 case
already covered in test_task10_connect_recovery.py.
"""

from __future__ import annotations

from playwright.sync_api import expect

from tests.browser.conftest import SEEDED_DEVICE_ID


def _card(page):
    return page.locator(f'[data-card="{SEEDED_DEVICE_ID}"]')


def _try_connect(page):
    card = _card(page)
    card.locator("[data-pw]").fill("some-password-123")
    card.locator("[data-connect]").click()
    return card


def test_connect_recovers_from_json_500_with_generic_detail_body(
    logged_in_page_with_device, app_server_with_device
):
    page = logged_in_page_with_device
    page.route(
        "**/api/ssh/connect",
        lambda route: route.fulfill(
            status=500,
            content_type="application/json",
            body='{"detail":"Internal server error."}',
        ),
    )
    card = _try_connect(page)
    connect_btn = card.locator("[data-connect]")
    expect(connect_btn).to_be_enabled()
    expect(connect_btn).to_have_text("Connect")
    err = card.locator("[data-cerr]")
    expect(err).to_contain_text("The server hit an error")
    expect(err).not_to_contain_text("Internal server error.")
    expect(err).not_to_be_empty()
