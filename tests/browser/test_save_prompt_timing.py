"""Browser coverage for when custom-command save offers appear."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import SEEDED_DEVICE_ID
from tests.browser.test_live_terminal import _box, _messages, _open_connected_page, _send, _start, _wait_for


def _run_custom_command(card, command: str) -> dict:
    card.locator("[data-custom]").fill(command)
    with card.page.expect_request("**/api/ssh/run") as request_info:
        card.locator("[data-run]").click()
    return request_info.value.post_data_json


@pytest.mark.browser
def test_custom_save_offer_waits_for_terminal_end_without_stealing_input(page, app_server_with_device):
    card, routes = _open_connected_page(page, app_server_with_device)
    page.route("**/api/ssh/run", lambda route: route.fulfill(status=200, content_type="application/json", body='{"ok": true, "run_id": "save-after-end"}'))

    request = _run_custom_command(card, "sudo custom-command")
    _start(routes, run_id="save-after-end", cmd_id=request["cmd_id"])
    box = _box(page, run_id="save-after-end")
    expect(page.locator("#saveModal")).not_to_have_class(re.compile(r"\bshow\b"))
    box.locator(".terminal-frame").click()
    assert _wait_for(page, lambda: len(_messages(routes, "term_claim"))) == 1
    _send(routes, {"type": "term_control", "device_id": SEEDED_DEVICE_ID, "run_id": "save-after-end", "yours": True, "rev": 1})
    page.keyboard.type("secret-text")
    page.keyboard.press("Enter")
    assert _wait_for(page, lambda: "".join(message["data"] for message in _messages(routes, "term_input"))) == "secret-text\r"
    expect(page.locator("#saveName")).to_have_value("")
    expect(page.locator("#saveModal")).not_to_have_class(re.compile(r"\bshow\b"))

    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "save-after-end"})
    expect(box).to_have_count(0)
    expect(page.locator("#saveModal")).to_have_class(re.compile(r"\bshow\b"))
    expect(page.locator("#savePreview")).to_have_text("sudo custom-command")


@pytest.mark.browser
def test_custom_save_offer_handles_run_reply_after_terminal_end_once(page, app_server_with_device):
    card, routes = _open_connected_page(page, app_server_with_device)
    pending = []
    page.route("**/api/ssh/run", lambda route: pending.append(route))

    request = _run_custom_command(card, "printf short")
    _start(routes, run_id="short-run", cmd_id=request["cmd_id"])
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "short-run", "seq": 1, "data": "done\r\n"})
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "short-run"})
    expect(_box(page, run_id="short-run")).to_have_count(0)
    expect(page.locator("#saveModal")).to_have_class(re.compile(r"\bshow\b"))
    page.locator("#saveSkip").click()
    expect(page.locator("#saveModal")).not_to_have_class(re.compile(r"\bshow\b"))
    pending[0].fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "run_id": "short-run"}))
    page.wait_for_timeout(500)
    expect(page.locator("#saveModal")).not_to_have_class(re.compile(r"\bshow\b"))


@pytest.mark.browser
def test_custom_save_offer_skipped_while_another_dialog_is_open(page, app_server_with_device):
    card, routes = _open_connected_page(page, app_server_with_device)
    page.route("**/api/ssh/run", lambda route: route.fulfill(status=200, content_type="application/json", body='{"ok": true, "run_id": "busy-dialog"}'))

    request = _run_custom_command(card, "printf dialog")
    _start(routes, run_id="busy-dialog", cmd_id=request["cmd_id"])
    _send(routes, {"type": "ssh_status", "device_id": SEEDED_DEVICE_ID, "state": "running"})
    page.locator(f'.tab[data-tab="{SEEDED_DEVICE_ID}"] .tclose').click()
    expect(page.locator("#confirm")).to_have_class(re.compile(r"\bshow\b"))
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "busy-dialog"})
    expect(_box(page, run_id="busy-dialog")).to_have_count(0)
    page.wait_for_timeout(300)
    expect(page.locator("#saveModal")).not_to_have_class(re.compile(r"\bshow\b"))
    expect(page.locator("#confirm")).to_have_class(re.compile(r"\bshow\b"))


@pytest.mark.browser
def test_failed_custom_run_offers_save_immediately(page, app_server_with_device):
    card, _ = _open_connected_page(page, app_server_with_device)
    page.route("**/api/ssh/run", lambda route: route.fulfill(status=409, content_type="application/json", body='{"ok": false, "error": "Device is busy."}'))

    _run_custom_command(card, "printf failed")
    expect(page.locator("#saveModal")).to_have_class(re.compile(r"\bshow\b"))
    expect(page.locator("#savePreview")).to_have_text("printf failed")


@pytest.mark.browser
def test_idle_mid_run_discards_custom_save_offer(page, app_server_with_device):
    card, routes = _open_connected_page(page, app_server_with_device)
    page.route("**/api/ssh/run", lambda route: route.fulfill(status=200, content_type="application/json", body='{"ok": true, "run_id": "idle-run"}'))

    request = _run_custom_command(card, "printf idle")
    _start(routes, run_id="idle-run", cmd_id=request["cmd_id"])
    _send(routes, {"type": "ssh_status", "device_id": SEEDED_DEVICE_ID, "state": "idle"})
    expect(_box(page, run_id="idle-run")).to_have_count(0)
    expect(page.locator("#saveModal")).not_to_have_class(re.compile(r"\bshow\b"))
