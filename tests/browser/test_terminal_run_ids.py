"""Browser run-id tracking for interactive terminal cancellation."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import DASHBOARD_PASSWORD, SEEDED_DEVICE_ID


def _open_connected_page(page, app_server_with_device):
    routes = []

    def handle_ws(ws) -> None:
        server = ws.connect_to_server()
        ws.on_message(lambda message: server.send(message))

        def forward(message) -> None:
            msg = json.loads(message)
            if msg.get("type") == "init":
                msg["ssh_connected"] = [SEEDED_DEVICE_ID]
                msg["ssh_running"] = []
                msg["ssh_terminals"] = []
                message = json.dumps(msg)
            ws.send(message)

        server.on_message(forward)
        routes.append(ws)

    page.route_web_socket(re.compile(r"/ws"), handle_ws)
    page.goto(f"{app_server_with_device}/login")
    page.fill("#password", DASHBOARD_PASSWORD)
    page.click("#submit")
    page.wait_for_url(f"{app_server_with_device}/")
    card = page.locator(f'[data-card="{SEEDED_DEVICE_ID}"]')
    expect(card).to_have_class(re.compile(r"\bssh-on\b"))
    return card, routes


def _send(routes, message: dict) -> None:
    routes[-1].send(json.dumps(message))


def _start_custom_command(card) -> None:
    card.locator("[data-custom]").fill("printf test")
    card.locator("[data-run]").click()


def _save_prompt_is_hidden_while_running(page) -> None:
    expect(page.locator("#saveModal")).not_to_have_class(re.compile(r"\bshow\b"))


@pytest.mark.browser
def test_stop_uses_run_id_from_run_reply_before_terminal_start(page, app_server_with_device):
    card, routes = _open_connected_page(page, app_server_with_device)
    page.route("**/api/ssh/run", lambda route: route.fulfill(status=200, content_type="application/json", body='{"ok": true, "run_id": "run-reply"}'))
    page.route("**/api/ssh/cancel", lambda route: route.fulfill(status=200, content_type="application/json", body='{"ok": true}'))

    # No term_start at all: the reply is the only source of the run_id.
    _start_custom_command(card)
    _save_prompt_is_hidden_while_running(page)
    _send(routes, {"type": "ssh_status", "device_id": SEEDED_DEVICE_ID, "state": "running"})

    with page.expect_request("**/api/ssh/cancel") as request_info:
        card.locator("[data-cancel]").click()
    assert request_info.value.post_data_json == {"device_id": SEEDED_DEVICE_ID, "run_id": "run-reply"}


@pytest.mark.browser
def test_stop_uses_run_id_when_terminal_starts_before_run_reply(page, app_server_with_device):
    card, routes = _open_connected_page(page, app_server_with_device)
    pending = []
    page.route("**/api/ssh/run", lambda route: pending.append(route))
    page.route("**/api/ssh/cancel", lambda route: route.fulfill(status=200, content_type="application/json", body='{"ok": true}'))

    with page.expect_request("**/api/ssh/run"):
        _start_custom_command(card)
    assert len(pending) == 1
    _send(routes, {"type": "term_start", "device_id": SEEDED_DEVICE_ID, "run_id": "run-start", "cmd_id": "cmd-one", "cols": 80, "rows": 24})
    _send(routes, {"type": "ssh_status", "device_id": SEEDED_DEVICE_ID, "state": "running"})

    # The reply is still held: term_start is the only source of the run_id.
    with page.expect_request("**/api/ssh/cancel") as request_info:
        card.locator("[data-cancel]").click()
    assert request_info.value.post_data_json == {"device_id": SEEDED_DEVICE_ID, "run_id": "run-start"}

    pending[0].fulfill(status=200, content_type="application/json", body='{"ok": true, "run_id": "run-start"}')
    _save_prompt_is_hidden_while_running(page)


@pytest.mark.browser
def test_late_run_reply_does_not_restore_an_ended_run(page, app_server_with_device):
    card, routes = _open_connected_page(page, app_server_with_device)
    pending = []
    cancel_requests = []
    page.route("**/api/ssh/run", lambda route: pending.append(route))
    page.route("**/api/ssh/cancel", lambda route: cancel_requests.append(route))

    with page.expect_request("**/api/ssh/run"):
        _start_custom_command(card)
    _send(routes, {"type": "ssh_status", "device_id": SEEDED_DEVICE_ID, "state": "running"})
    _send(routes, {"type": "term_start", "device_id": SEEDED_DEVICE_ID, "run_id": "run-late", "cmd_id": "cmd-one", "cols": 80, "rows": 24})
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "run-late"})
    pending[0].fulfill(status=200, content_type="application/json", body='{"ok": true, "run_id": "run-late"}')

    card.locator("[data-cancel]").click()
    expect(page.locator(f'[data-console="{SEEDED_DEVICE_ID}"] .ln.err .msg')).to_have_text(
        "Could not stop: this page does not know which command is running. Refresh the page and try again."
    )
    assert not cancel_requests


@pytest.mark.browser
def test_stop_without_a_known_run_id_adds_an_error_line(page, app_server_with_device):
    card, routes = _open_connected_page(page, app_server_with_device)
    cancel_requests = []
    page.route("**/api/ssh/cancel", lambda route: cancel_requests.append(route))

    _send(routes, {"type": "term_start", "device_id": SEEDED_DEVICE_ID, "run_id": "run-ended", "cmd_id": "cmd-one", "cols": 80, "rows": 24})
    _send(routes, {"type": "ssh_status", "device_id": SEEDED_DEVICE_ID, "state": "running"})
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "run-ended"})
    card.locator("[data-cancel]").click()

    expect(page.locator(f'[data-console="{SEEDED_DEVICE_ID}"] .ln.err .msg')).to_have_text(
        "Could not stop: this page does not know which command is running. Refresh the page and try again."
    )
    assert not cancel_requests


@pytest.mark.browser
def test_second_run_replaces_the_previous_run_id(page, app_server_with_device):
    card, routes = _open_connected_page(page, app_server_with_device)
    run_ids = iter(("run-first", "run-second"))
    page.route("**/api/ssh/run", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "run_id": next(run_ids)})))
    page.route("**/api/ssh/cancel", lambda route: route.fulfill(status=200, content_type="application/json", body='{"ok": true}'))

    _start_custom_command(card)
    _send(routes, {"type": "term_start", "device_id": SEEDED_DEVICE_ID, "run_id": "run-first", "cmd_id": "cmd-one", "cols": 80, "rows": 24})
    _send(routes, {"type": "ssh_status", "device_id": SEEDED_DEVICE_ID, "state": "connected"})
    _start_custom_command(card)
    _send(routes, {"type": "term_start", "device_id": SEEDED_DEVICE_ID, "run_id": "run-second", "cmd_id": "cmd-two", "cols": 80, "rows": 24})
    page.locator("#saveSkip").click()
    _send(routes, {"type": "ssh_status", "device_id": SEEDED_DEVICE_ID, "state": "running"})

    with page.expect_request("**/api/ssh/cancel") as request_info:
        card.locator("[data-cancel]").click()
    assert request_info.value.post_data_json == {"device_id": SEEDED_DEVICE_ID, "run_id": "run-second"}
