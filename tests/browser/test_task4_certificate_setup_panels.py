"""The certificate setup page shows independent per-OS panels and both themes."""

from __future__ import annotations

from playwright.sync_api import expect


def test_panels_start_collapsed_and_open_independently(_chromium_available, page, app_server):
    base_url = app_server
    page.goto(f"{base_url}/certificate-setup")

    expect(page.locator("h1")).to_have_text("Set up certificate trust")

    # Exactly three per-OS panels, in order.
    expect(page.locator("summary")).to_have_text(["Windows", "Linux", "macOS"])

    # All three panels are closed on load.
    assert page.locator("details").count() == 3
    assert page.locator("details[open]").count() == 0

    # Opening one panel does not close another: they are independent, not an accordion.
    page.locator("summary", has_text="Windows").click()
    page.locator("summary", has_text="Linux").click()
    assert page.locator("details[open]").count() == 2


def test_theme_toggle_switches_and_persists(_chromium_available, page, app_server):
    base_url = app_server
    page.goto(f"{base_url}/certificate-setup")

    html = page.locator("html")
    expect(html).not_to_have_class("light")

    page.locator("#themeToggle").click()
    expect(html).to_have_class("light")
    assert page.evaluate("localStorage.getItem('snd-theme')") == "light"

    # The stored choice survives a reload.
    page.reload()
    expect(page.locator("html")).to_have_class("light")

    page.locator("#themeToggle").click()
    expect(page.locator("html")).not_to_have_class("light")
    assert page.evaluate("localStorage.getItem('snd-theme')") == "dark"
