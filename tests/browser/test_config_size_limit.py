"""A command save rejected for exceeding the total configuration size cap.

The server refuses a save that would push the whole saved configuration past
MAX_CONFIG_BYTES. persistCommands() in static/index.html then shows the server
message in the error banner, keeps the modal open with its inline error, and
rolls the optimistic local edit back from the server. A fresh page load still
works afterward.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import expect

from simple_network_dashboard.config import MAX_CONFIG_BYTES
from tests.browser.conftest import SEEDED_DEVICE_ID

BIG = 3000
LIMIT_TEXT = "larger than the 1 MB limit"


def _big_commands(count: int, prefix: str) -> list:
    return [{"name": f"{prefix}{i}", "command": "x" * BIG, "sudo": False, "confirm": ""} for i in range(count)]


def _fill_near_cap(page) -> int:
    """Fill the seeded device and a second device until under 3 KB remains.

    Returns how many commands the seeded device holds afterward.
    """
    seeded_commands = _big_commands(249, "a")
    result = page.evaluate(
        "args => PUT(`/api/devices/${args.id}/commands`, {commands: args.commands})",
        {"id": SEEDED_DEVICE_ID, "commands": seeded_commands},
    )
    assert result["ok"], result
    second = page.evaluate(
        """body => POST('/api/devices', body)""",
        {"name": "Second", "host": "10.0.0.98", "username": "tester", "metrics_port": 9100, "commands": []},
    )
    assert second["ok"], second
    second_id = next(d["id"] for d in second["devices"] if d["name"] == "Second")
    size = len(json.dumps(second["devices"]))
    count = (MAX_CONFIG_BYTES - size - 3500) // (BIG + 60)
    filled = page.evaluate(
        "args => PUT(`/api/devices/${args.id}/commands`, {commands: args.commands})",
        {"id": second_id, "commands": _big_commands(count, "b")},
    )
    assert filled["ok"], filled
    size = len(json.dumps(filled["devices"]))
    pad_len = MAX_CONFIG_BYTES - size - 2000 - 80
    assert 0 < pad_len <= 4096, pad_len
    commands = _big_commands(count, "b") + [
        {"name": "pad", "command": "y" * pad_len, "sudo": False, "confirm": ""}
    ]
    padded = page.evaluate(
        "args => PUT(`/api/devices/${args.id}/commands`, {commands: args.commands})",
        {"id": second_id, "commands": commands},
    )
    assert padded["ok"], padded
    final_size = len(json.dumps(padded["devices"]))
    assert MAX_CONFIG_BYTES - 3000 < final_size <= MAX_CONFIG_BYTES, final_size
    return len(seeded_commands)


def _seeded_command_count(page) -> int:
    return page.evaluate(
        "id => DEVICES.find(d => d.id === id).commands.length", SEEDED_DEVICE_ID)


def test_size_rejected_command_save_shows_error_and_rolls_back(
    logged_in_page_with_device, app_server_with_device
):
    page = logged_in_page_with_device
    before = _fill_near_cap(page)
    page.reload()
    expect(page.locator(f'[data-card="{SEEDED_DEVICE_ID}"]')).to_be_visible()

    page.click(f'[data-card="{SEEDED_DEVICE_ID}"]')
    page.click("#addCmdBtn")
    page.fill("#cName", "Too Big")
    page.fill("#cCmd", "z" * BIG)
    page.click("#cmdSave")

    expect(page.locator("#errorBanners .error-banner .eb-text")).to_contain_text(LIMIT_TEXT)
    expect(page.locator("#cmdErr")).to_contain_text("Save failed")
    expect(page.locator("#cmdModal")).to_have_class(re.compile(r"\bshow\b"))
    assert _seeded_command_count(page) == before
    assert page.evaluate(
        "id => DEVICES.find(d => d.id === id).commands.some(c => c.name === 'Too Big')",
        SEEDED_DEVICE_ID,
    ) is False

    page.click("#cmdCancel")
    page.reload()
    expect(page.locator(f'[data-card="{SEEDED_DEVICE_ID}"]')).to_be_visible()
    page.click(f'[data-card="{SEEDED_DEVICE_ID}"]')
    assert _seeded_command_count(page) == before
