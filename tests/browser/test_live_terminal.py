"""Browser coverage for the view-only live terminal box."""

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


def _start(routes, device_id=SEEDED_DEVICE_ID, run_id="run-one", cmd_id="other", cols=80, rows=24) -> None:
    _send(routes, {"type": "term_start", "device_id": device_id, "run_id": run_id, "cmd_id": cmd_id, "cols": cols, "rows": rows})


def _box(page, device_id=SEEDED_DEVICE_ID, run_id="run-one"):
    return page.locator(f'[data-console="{device_id}"] .live-terminal[data-run-id="{run_id}"]')


@pytest.mark.browser
def test_run_request_has_measured_size_and_hidden_console_falls_back(page, app_server_with_device):
    card, _ = _open_connected_page(page, app_server_with_device)
    page.route("**/api/ssh/run", lambda route: route.fulfill(status=200, content_type="application/json", body='{"ok": true, "run_id": "one"}'))
    card.locator("[data-custom]").fill("printf test")
    with page.expect_request("**/api/ssh/run") as request_info:
        card.locator("[data-run]").click()
    payload = request_info.value.post_data_json
    assert 20 <= payload["cols"] <= 500
    assert 5 <= payload["rows"] <= 200
    page.locator("#saveSkip").click()

    page.evaluate("() => { document.querySelector('#consoleBody').style.display = 'none'; }")
    card.locator("[data-custom]").fill("printf hidden")
    with page.expect_request("**/api/ssh/run") as request_info:
        card.locator("[data-run]").click()
    assert request_info.value.post_data_json["cols"] == 120
    assert request_info.value.post_data_json["rows"] == 30


@pytest.mark.browser
def test_live_box_focus_output_filtering_and_end(page, app_server_with_device):
    card, routes = _open_connected_page(page, app_server_with_device)
    page.route("**/api/ssh/run", lambda route: route.fulfill(status=200, content_type="application/json", body='{"ok": true, "run_id": "mine"}'))
    card.locator("[data-custom]").fill("printf mine")
    with page.expect_request("**/api/ssh/run") as request_info:
        card.locator("[data-run]").click()
    page.locator("#saveSkip").click()
    _start(routes, run_id="mine", cmd_id=request_info.value.post_data_json["cmd_id"])
    mine = _box(page, run_id="mine")
    expect(mine).to_have_class(re.compile(r"\bterminal-focused\b"))
    expect(mine.locator(".terminal-strip")).to_have_text("Running. Your typing goes to this command.")
    page.locator("#theme-btn").click()
    assert mine.locator(".terminal-frame").evaluate("element => getComputedStyle(element).backgroundColor") != ""
    page.locator("#libTitle").click()
    expect(mine.locator(".terminal-strip")).to_have_text("Running. Click in this window to type.")
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "wrong", "seq": 1, "data": "wrong"})
    _send(routes, {"type": "term_data", "device_id": "other", "run_id": "other", "seq": 1, "data": "other"})
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "mine", "seq": 1, "data": "right\r\n"})
    expect(mine.locator(".xterm-rows")).to_contain_text("right")
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "mine"})
    expect(mine.locator(".terminal-strip")).to_have_count(0)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "mine", "seq": 2, "data": "late"})
    expect(mine.locator(".xterm-rows")).not_to_contain_text("late")
    _start(routes, run_id="second")
    expect(_box(page, run_id="second")).to_have_count(1)
    expect(_box(page, run_id="mine")).to_have_count(1)


@pytest.mark.browser
def test_terminal_frame_scrolling_and_full_screen(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes, rows=8)
    box = _box(page)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": "one\r\ntwo\r\n"})
    expect(box.locator(".xterm-rows")).to_contain_text("two")
    small_height = box.locator(".terminal-frame").evaluate("element => element.getBoundingClientRect().height")
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 2, "data": "x\r\n" * 20})
    expect(box).to_have_class(re.compile(r"\bterminal-scrollback\b"))
    full_height = box.locator(".terminal-frame").evaluate("element => element.getBoundingClientRect().height")
    assert full_height > small_height
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 3, "data": "\x1b[?1049h"})
    expect(box).to_have_class(re.compile(r"\bterminal-fullscreen\b"))
    expect(box).not_to_have_class(re.compile(r"\bterminal-scrollback\b"))
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 4, "data": "\x1b[?1049l"})
    expect(box).not_to_have_class(re.compile(r"\bterminal-fullscreen\b"))


@pytest.mark.browser
def test_terminal_stays_visible_on_resize_hidden_tab_and_line_trim(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes, rows=30)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": "line\r\n" * 40})
    _start(routes, device_id="other-device", run_id="other-run", rows=20)
    _send(routes, {"type": "term_data", "device_id": "other-device", "run_id": "other-run", "seq": 1, "data": "hidden output\r\n"})
    page.locator('[data-tab="other-device"]').click()
    other = _box(page, "other-device", "other-run")
    expect(other.locator(".xterm-rows")).to_contain_text("hidden output")
    for size in ({"width": 1280, "height": 800}, {"width": 1024, "height": 600}, {"width": 800, "height": 500}):
        page.set_viewport_size(size)
        visible = other.evaluate("box => { const con = box.closest('.console'); const b = box.getBoundingClientRect(); const c = con.getBoundingClientRect(); return b.bottom <= c.bottom + 1; }")
        assert visible, size
    for index in range(1600):
        _send(routes, {"type": "ssh_log", "device_id": "other-device", "text": f"app {index}", "level": "out"})
    expect(other).to_have_count(1)


@pytest.mark.browser
def test_large_burst_reports_terminal_geometry(page, app_server_with_device, capsys):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes, rows=40)
    box = _box(page)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": "line\r\n" * 3000})
    expect(box).to_have_class(re.compile(r"\bterminal-scrollback\b"))
    before = box.evaluate("box => ({pixels: box.querySelector('.terminal-frame').getBoundingClientRect().height, rows: Math.round((box.querySelector('.terminal-frame').getBoundingClientRect().height - 4) / box.querySelector('.xterm-rows > div').getBoundingClientRect().height), bottomVisible: box.getBoundingClientRect().bottom <= box.closest('.console').getBoundingClientRect().bottom + 1})")
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 2, "data": "\x1b[?1049h"})
    full = box.evaluate("box => ({pixels: box.querySelector('.terminal-frame').getBoundingClientRect().height, rows: Math.round((box.querySelector('.terminal-frame').getBoundingClientRect().height - 4) / box.querySelector('.xterm-rows > div').getBoundingClientRect().height), bottomVisible: box.getBoundingClientRect().bottom <= box.closest('.console').getBoundingClientRect().bottom + 1})")
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 3, "data": "\x1b[?1049l"})
    after = box.evaluate("box => ({pixels: box.querySelector('.terminal-frame').getBoundingClientRect().height, rows: Math.round((box.querySelector('.terminal-frame').getBoundingClientRect().height - 4) / box.querySelector('.xterm-rows > div').getBoundingClientRect().height), bottomVisible: box.getBoundingClientRect().bottom <= box.closest('.console').getBoundingClientRect().bottom + 1})")
    assert before["bottomVisible"] and full["bottomVisible"] and after["bottomVisible"]
    print(f"extra terminal geometry: burst={before}, alternate-open={full}, alternate-close={after}")


@pytest.mark.browser
def test_run_started_elsewhere_after_own_run_is_not_focused(page, app_server_with_device):
    card, routes = _open_connected_page(page, app_server_with_device)
    page.route("**/api/ssh/run", lambda route: route.fulfill(status=200, content_type="application/json", body='{"ok": true, "run_id": "mine"}'))
    card.locator("[data-custom]").fill("printf mine")
    with page.expect_request("**/api/ssh/run") as request_info:
        card.locator("[data-run]").click()
    page.locator("#saveSkip").click()
    _start(routes, run_id="mine", cmd_id=request_info.value.post_data_json["cmd_id"])
    expect(_box(page, run_id="mine")).to_have_class(re.compile(r"\bterminal-focused\b"))
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "mine"})

    # Another tab of the same browser starts the next run: its cmd_id is not ours.
    _start(routes, run_id="theirs", cmd_id="c-from-another-tab")
    theirs = _box(page, run_id="theirs")
    expect(theirs.locator(".terminal-strip")).to_have_text("Running. Click in this window to type.")
    expect(theirs).not_to_have_class(re.compile(r"\bterminal-focused\b"))


@pytest.mark.browser
def test_keyboard_focus_change_updates_strip(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    box = _box(page)
    box.locator(".terminal-frame").click()
    expect(box.locator(".terminal-strip")).to_have_text("Running. Your typing goes to this command.")
    page.evaluate("() => document.activeElement.blur()")
    expect(box.locator(".terminal-strip")).to_have_text("Running. Click in this window to type.")
    expect(box).not_to_have_class(re.compile(r"\bterminal-focused\b"))


@pytest.mark.browser
def test_full_screen_switch_split_across_batches_is_detected(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes, rows=8)
    box = _box(page)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": "one\r\n\x1b[?10"})
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 2, "data": "49h"})
    expect(box).to_have_class(re.compile(r"\bterminal-fullscreen\b"))
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 3, "data": "\x1b[?104"})
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 4, "data": "9l"})
    expect(box).not_to_have_class(re.compile(r"\bterminal-fullscreen\b"))


@pytest.mark.browser
def test_terminal_taller_than_console_shows_its_newest_row(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    page.set_viewport_size({"width": 1024, "height": 600})
    _start(routes, rows=60)
    box = _box(page)
    lines = "".join(f"row {n:02d}\r\n" for n in range(1, 50)) + "row 50 newest"
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": lines})
    expect(box.locator(".xterm-rows")).to_contain_text("row 50 newest")
    inside = box.evaluate(
        """box => {
            const frame = box.querySelector('.terminal-frame').getBoundingClientRect();
            const row = [...box.querySelectorAll('.xterm-rows > div')].find(r => r.textContent.includes('row 50 newest'));
            const r = row.getBoundingClientRect();
            return r.top >= frame.top - 1 && r.bottom <= frame.bottom + 1;
        }"""
    )
    assert inside
