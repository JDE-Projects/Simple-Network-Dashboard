"""Browser coverage for device cards and the dashboard grid in short windows."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect


def _add_devices(page, count: int = 5) -> None:
    """Add enough cards through the dashboard UI to overflow the device list."""
    for index in range(count):
        page.click("#addBtn")
        page.fill("#fName", f"Layout Test Device {index + 1}")
        page.fill("#fHost", f"10.0.1.{index + 1}")
        page.fill("#fUser", "layouttester")
        page.click("#devSave")
        expect(page.locator("#deviceModal")).not_to_have_class(re.compile(r"\bshow\b"))

    expect(page.locator("#deviceList > .card")).to_have_count(count)


@pytest.mark.browser
def test_device_cards_keep_full_height_and_device_list_scrolls(logged_in_page):
    page = logged_in_page
    _add_devices(page)
    page.set_viewport_size({"width": 1280, "height": 600})

    measurements = page.locator("#deviceList").evaluate(
        """list => ({
            listClientHeight: list.clientHeight,
            listScrollHeight: list.scrollHeight,
            cards: [...list.querySelectorAll(':scope > .card')].map(card => ({
                clientHeight: card.clientHeight,
                scrollHeight: card.scrollHeight,
            })),
        })"""
    )

    assert measurements["listScrollHeight"] > measurements["listClientHeight"]
    assert all(
        card["scrollHeight"] <= card["clientHeight"] for card in measurements["cards"]
    ), "a device card was compressed below the height of its contents"


@pytest.mark.browser
def test_short_window_keeps_middle_row_minimum_and_page_scrolls(logged_in_page):
    page = logged_in_page
    _add_devices(page)
    page.set_viewport_size({"width": 1280, "height": 400})

    measurements = page.evaluate(
        """() => ({
            devicesHeight: document.querySelector('.devices').getBoundingClientRect().height,
            bodyClientHeight: document.body.clientHeight,
            bodyScrollHeight: document.body.scrollHeight,
        })"""
    )

    assert measurements["devicesHeight"] >= 320
    assert measurements["bodyScrollHeight"] > measurements["bodyClientHeight"]
