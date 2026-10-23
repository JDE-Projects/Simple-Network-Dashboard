"""Browser coverage for the console's time column fitting the local time format."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import SEEDED_DEVICE_ID
from tests.browser.test_live_terminal import _box, _open_connected_page, _start


def _layout(page, app_server_with_device) -> dict:
    """Open a live terminal box, add a console line, and measure both."""
    page.clock.set_fixed_time("2024-01-01T17:04:34Z")
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    expect(_box(page)).to_have_count(1)
    page.evaluate("id => addLine(id, 'layout check', 'out')", SEEDED_DEVICE_ID)
    tag = page.locator(f'[data-console="{SEEDED_DEVICE_ID}"] .ln .tag').last
    expect(tag).not_to_have_text("")
    return tag.evaluate(
        """tag => {
            const box = tag.closest('.console').querySelector('.live-terminal');
            return {
                text: tag.textContent,
                clientWidth: tag.clientWidth,
                scrollWidth: tag.scrollWidth,
                messageLeft: tag.nextElementSibling.getBoundingClientRect().left,
                boxLeft: box.getBoundingClientRect().left,
            };
        }"""
    )


@pytest.mark.browser
@pytest.mark.browser_context_args(locale="en-US", timezone_id="UTC")
def test_twelve_hour_time_shows_am_pm_in_full(page, app_server_with_device):
    layout = _layout(page, app_server_with_device)
    assert layout["text"] == "05:04:34 PM"
    assert layout["scrollWidth"] <= layout["clientWidth"], "the time is cut off"
    assert layout["clientWidth"] > 64
    assert abs(layout["boxLeft"] - layout["messageLeft"]) <= 1


@pytest.mark.browser
@pytest.mark.browser_context_args(locale="en-GB", timezone_id="UTC")
def test_twenty_four_hour_time_keeps_the_original_width(page, app_server_with_device):
    layout = _layout(page, app_server_with_device)
    assert layout["text"] == "17:04:34"
    assert layout["scrollWidth"] <= layout["clientWidth"], "the time is cut off"
    assert layout["clientWidth"] == 64
    assert abs(layout["boxLeft"] - layout["messageLeft"]) <= 1
