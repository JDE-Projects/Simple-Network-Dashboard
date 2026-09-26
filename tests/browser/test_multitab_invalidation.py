"""Multi-tab session revocation opens the sign-in modal in every live tab."""

from __future__ import annotations

import re

from playwright.sync_api import expect

from auth import create_auth_state, publish_auth_state
from tests.browser.conftest import DASHBOARD_PASSWORD


def _sign_in(page, base_url: str) -> None:
    """Sign in and wait until the dashboard's live socket has initialized."""
    page.goto(f"{base_url}/login")
    page.fill("#password", DASHBOARD_PASSWORD)
    page.click("#submit")
    page.wait_for_url(f"{base_url}/")
    expect(page.locator("#verLabel")).to_have_text(re.compile(r"^v"))


def _expect_session_expired(page, base_url: str) -> None:
    """Assert that a live tab remains in place and offers a sign-in action."""
    expect(page.locator("#sessionExpiredModal")).to_have_class(
        re.compile(r"\bshow\b"), timeout=5_000
    )
    expect(page.locator("#sessionExpiredSignIn")).to_be_visible()
    assert page.url.rstrip("/") == base_url.rstrip("/"), "must not auto-redirect on session expiry"


def test_signing_out_one_tab_expires_another_live_tab(
    _chromium_available,
    page,
    app_server_with_short_ws_revalidation,
):
    base_url = app_server_with_short_ws_revalidation
    _sign_in(page, base_url)

    other_page = page.context.new_page()
    other_page.goto(f"{base_url}/")
    expect(other_page.locator("#verLabel")).to_have_text(re.compile(r"^v"))

    page.click("#signOutBtn")
    page.wait_for_url(f"{base_url}/login")

    _expect_session_expired(other_page, base_url)


def test_password_reset_expires_every_live_tab(
    _chromium_available,
    page,
    tmp_path,
    app_server_with_short_ws_revalidation,
):
    base_url = app_server_with_short_ws_revalidation
    _sign_in(page, base_url)

    second_page = page.context.new_page()
    second_page.goto(f"{base_url}/")
    expect(second_page.locator("#verLabel")).to_have_text(re.compile(r"^v"))

    third_page = page.context.new_page()
    third_page.goto(f"{base_url}/")
    expect(third_page.locator("#verLabel")).to_have_text(re.compile(r"^v"))

    publish_auth_state(
        tmp_path / "data" / "auth.json",
        create_auth_state(DASHBOARD_PASSWORD),
    )

    for live_page in (page, second_page, third_page):
        _expect_session_expired(live_page, base_url)
