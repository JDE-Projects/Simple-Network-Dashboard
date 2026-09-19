"""Phase 1 proof: the HTTPS harness serves the app, login works, and the
live WebSocket delivers the dashboard's initial state."""

from __future__ import annotations

from tests.browser.conftest import DASHBOARD_PASSWORD


def test_unauthenticated_root_redirects_to_login(_chromium_available, page, app_server):
    page.goto(f"{app_server}/")
    page.wait_for_url(f"{app_server}/login")
    assert page.is_visible("#loginForm")


def test_login_loads_dashboard_over_websocket(logged_in_page, app_server):
    page = logged_in_page
    # The version label is only filled from the WebSocket 'init' payload.
    assert page.text_content("#verLabel").startswith("v")
    # A fresh install has no devices; this text is rendered by init as well.
    assert "No devices yet" in page.text_content("#deviceList")


def test_wrong_password_stays_on_login(_chromium_available, page, app_server):
    page.goto(f"{app_server}/login")
    page.fill("#password", DASHBOARD_PASSWORD + "-wrong")
    page.click("#submit")
    page.wait_for_selector("#error:not(:empty)")
    assert page.url.endswith("/login")
