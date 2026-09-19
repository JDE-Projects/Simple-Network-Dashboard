"""Task 10 coverage: custom-command run and host-key view recover from API
failures.

Custom command: static/index.html's runCustom() (~line 1522) clears the
input immediately (optimistic), then restores the typed text on failure.
Uses connected_device_page because the custom-command input only appears on
a card once it looks SSH-connected (see conftest.py for how that's faked
without a real SSH target).

Host-key view: the [data-hostkey] button's handler (~line 1436) shows an
error banner on failure and never renders a raw/undefined value.
"""

from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.browser.conftest import SEEDED_DEVICE_ID

CUSTOM_COMMAND = "echo custom-recovery-test"


def _card(page):
    return page.locator(f'[data-card="{SEEDED_DEVICE_ID}"]')


def _run_custom(page):
    card = _card(page)
    input_ = card.locator("[data-custom]")
    input_.fill(CUSTOM_COMMAND)
    card.locator("[data-run]").click()
    return input_


def test_custom_command_restores_typed_text_on_dropped_network(connected_device_page, app_server_with_device):
    page = connected_device_page
    page.route("**/api/ssh/run", lambda route: route.abort())
    input_ = _run_custom(page)
    expect(input_).to_have_value(CUSTOM_COMMAND)


def test_custom_command_restores_typed_text_on_server_error(connected_device_page, app_server_with_device):
    page = connected_device_page
    page.route(
        "**/api/ssh/run",
        lambda route: route.fulfill(status=500, content_type="text/plain", body="Internal Server Error"),
    )
    input_ = _run_custom(page)
    expect(input_).to_have_value(CUSTOM_COMMAND)
    expect(page.locator("body")).not_to_contain_text("Internal Server Error")


def test_custom_command_restores_typed_text_on_expired_session(connected_device_page, app_server_with_device):
    page = connected_device_page
    page.route(
        "**/api/ssh/run",
        lambda route: route.fulfill(status=401, content_type="application/json", body='{"detail":"unauthorized"}'),
    )
    input_ = _run_custom(page)
    expect(input_).to_have_value(CUSTOM_COMMAND)
    expect(page.locator("#sessionExpiredModal")).to_have_class(re.compile(r"\bshow\b"))


def test_custom_command_restores_typed_text_on_malformed_response(connected_device_page, app_server_with_device):
    # A 200 with an unparsable body is treated as a failure by api(), so
    # runCustom() takes its failure branch and restores the typed text, the
    # same as any other failed run. The raw body must not leak.
    page = connected_device_page
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.route(
        "**/api/ssh/run",
        lambda route: route.fulfill(status=200, content_type="application/json", body="not json{"),
    )
    input_ = _run_custom(page)
    expect(input_).to_have_value(CUSTOM_COMMAND)
    assert not errors
    expect(page.locator("body")).not_to_contain_text("not json")


def test_host_key_view_shows_error_on_dropped_network(logged_in_page_with_device, app_server_with_device):
    page = logged_in_page_with_device
    page.route("**/api/ssh/host_key/*", lambda route: route.abort())
    _card(page).locator("[data-hostkey]").click()
    banner = page.locator(".error-banner .eb-text")
    expect(banner).to_contain_text("Could not reach the server")


def test_host_key_view_shows_error_on_server_error(logged_in_page_with_device, app_server_with_device):
    page = logged_in_page_with_device
    page.route(
        "**/api/ssh/host_key/*",
        lambda route: route.fulfill(status=500, content_type="text/plain", body="Internal Server Error"),
    )
    _card(page).locator("[data-hostkey]").click()
    banner = page.locator(".error-banner .eb-text")
    expect(banner).to_contain_text("The server hit an error")
    expect(banner).not_to_contain_text("Internal Server Error")


def test_host_key_view_shows_error_on_expired_session(logged_in_page_with_device, app_server_with_device):
    page = logged_in_page_with_device
    page.route(
        "**/api/ssh/host_key/*",
        lambda route: route.fulfill(status=401, content_type="application/json", body='{"detail":"unauthorized"}'),
    )
    _card(page).locator("[data-hostkey]").click()
    expect(page.locator("#sessionExpiredModal")).to_have_class(re.compile(r"\bshow\b"))
    banner = page.locator(".error-banner .eb-text")
    expect(banner).to_contain_text("Your session has expired")


def test_host_key_view_shows_error_on_malformed_response(logged_in_page_with_device, app_server_with_device):
    # A 200 with an unparsable body is treated as a failure by api(), so the
    # handler shows an error banner instead of falling into its "no key pinned
    # yet" branch and rendering "No key pinned yet for undefined."
    page = logged_in_page_with_device
    page.route(
        "**/api/ssh/host_key/*",
        lambda route: route.fulfill(status=200, content_type="application/json", body="not json{"),
    )
    _card(page).locator("[data-hostkey]").click()
    banner = page.locator(".error-banner .eb-text")
    expect(banner).to_contain_text("unexpected response")
    expect(page.locator("body")).not_to_contain_text("undefined")
