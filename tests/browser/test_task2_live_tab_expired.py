"""A revoked live WebSocket session opens the existing expiration modal."""

from __future__ import annotations

import re

from playwright.sync_api import expect

import main


def test_revoked_live_socket_shows_session_expired_modal(
    _chromium_available,
    page,
    app_server_with_short_ws_revalidation,
):
    base_url = app_server_with_short_ws_revalidation
    page.goto(f"{base_url}/login")
    page.fill("#password", "harness-password-123")
    page.click("#submit")
    page.wait_for_url(f"{base_url}/")
    expect(page.locator("#verLabel")).to_have_text(re.compile(r"^v"))

    csrf_cookie = next(
        cookie
        for cookie in page.context.cookies()
        if cookie["name"] == main.CSRF_COOKIE_NAME
    )
    response = page.request.post(
        f"{base_url}/api/auth/logout",
        headers={"X-CSRF-Token": csrf_cookie["value"]},
    )
    assert response.ok

    modal = page.locator("#sessionExpiredModal")
    expect(modal).to_have_class(re.compile(r"\bshow\b"), timeout=5_000)
    expect(page.locator("#sessionExpiredSignIn")).to_be_visible()
    assert page.url.rstrip("/") == base_url.rstrip("/"), "must not auto-redirect on session expiry"
