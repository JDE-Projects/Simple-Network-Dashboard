"""Browser rendering for incomplete and invalid metrics payloads."""

import pytest
from playwright.sync_api import expect

from .conftest import SEEDED_DEVICE_ID


def _render_metrics(page, metrics):
    page.evaluate(
        """([deviceId, readings]) => {
            currentDeviceId = deviceId;
            METRICS[deviceId] = readings;
            renderStats();
        }""",
        [SEEDED_DEVICE_ID, metrics],
    )


@pytest.mark.browser
def test_panel_stays_live_with_reading_missing(logged_in_page_with_device):
    page = logged_in_page_with_device
    _render_metrics(page, {"cpu_count": 4})

    expect(page.locator("#statsPanel .live-chip")).to_have_text("Live")
    expect(page.locator("#statsPanel .stat-label", has_text="CPU")).to_be_visible()
    expect(page.locator("#statsPanel")).not_to_contain_text("undefined")


@pytest.mark.browser
def test_invalid_metrics_panel_wording(logged_in_page_with_device):
    page = logged_in_page_with_device
    _render_metrics(page, {"error": "invalid metrics"})

    expect(page.locator("#statsPanel .err-msg")).to_have_text(
        "Node Exporter on 10.0.0.99:9100 sent invalid readings"
    )
    expect(page.locator("#statsPanel .install-box")).to_have_count(0)


@pytest.mark.browser
def test_unreachable_panel_unchanged(logged_in_page_with_device):
    page = logged_in_page_with_device
    _render_metrics(page, {"error": "unreachable"})

    expect(page.locator("#statsPanel .err-msg")).to_have_text(
        "Node Exporter unreachable on 10.0.0.99:9100"
    )
    expect(page.locator("#statsPanel .install-box")).to_have_text(
        "sudo apt install prometheus-node-exporter"
    )
