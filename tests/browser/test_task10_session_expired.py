"""Task 10 coverage: a session-expired (401/403) response shows exactly one
modal (never stacks), offers a Sign In button, and never auto-redirects, so
anything the user was typing is still there.

See static/index.html's showSessionExpired() (~line 878, latched by the
_sessionExpiredShown flag so a second 401 is a no-op) and api()'s 401/403
branch (~line 921).
"""

from __future__ import annotations

import re

from playwright.sync_api import expect


def test_session_expired_modal_does_not_stack_and_retains_typed_values(logged_in_page, app_server):
    page = logged_in_page

    def unauthorized(route):
        route.fulfill(status=401, content_type="application/json", body='{"detail":"unauthorized"}')

    page.route(
        "**/api/devices",
        lambda route: unauthorized(route) if route.request.method == "POST" else route.continue_(),
    )
    page.route("**/api/check-update", unauthorized)

    # First failing call, from the device-save modal, with typed values still
    # in the open form.
    page.click("#addBtn")
    page.fill("#fName", "Still Typing")
    page.fill("#fHost", "10.0.0.150")
    page.fill("#fUser", "stilltyping")
    page.click("#devSave")

    modal = page.locator("#sessionExpiredModal")
    expect(modal).to_have_class(re.compile(r"\bshow\b"))
    expect(page.locator("#sessionExpiredSignIn")).to_be_visible()

    # Second, unrelated failing call. The modal is a blocking overlay by
    # design, so force the click through it the way a background call (e.g.
    # a poll already in flight) would still reach the API while it is open.
    # The modal must not stack or duplicate, and the app must not have
    # navigated away on its own.
    page.click("#updateBtn", force=True)
    page.wait_for_timeout(300)

    expect(modal).to_have_count(1)
    expect(modal).to_have_class(re.compile(r"\bshow\b"))
    expect(page.locator("#sessionExpiredSignIn")).to_be_visible()
    assert page.url.rstrip("/") == app_server.rstrip("/"), "must not auto-redirect on session expiry"

    # The typed values in the still-open device modal must have survived
    # both failures untouched.
    expect(page.locator("#deviceModal")).to_have_class(re.compile(r"\bshow\b"))
    expect(page.locator("#fName")).to_have_value("Still Typing")
    expect(page.locator("#fHost")).to_have_value("10.0.0.150")
    expect(page.locator("#fUser")).to_have_value("stilltyping")


def test_session_expired_via_real_cookie_clear(logged_in_page, app_server):
    """Complements the route-mocked test above with a real 401 from the
    actual server, produced by dropping the session cookie rather than
    faking the response.
    """
    page = logged_in_page
    page.context.clear_cookies()

    page.click("#addBtn")
    page.fill("#fName", "Cookie Cleared")
    page.fill("#fHost", "10.0.0.151")
    page.fill("#fUser", "cookiecleared")
    page.click("#devSave")

    expect(page.locator("#sessionExpiredModal")).to_have_class(re.compile(r"\bshow\b"))
    expect(page.locator("#sessionExpiredSignIn")).to_be_visible()
    expect(page.locator("#fName")).to_have_value("Cookie Cleared")
