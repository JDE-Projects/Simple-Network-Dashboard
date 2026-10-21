"""Browser coverage for the live terminal box: display, typing and control."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import DASHBOARD_PASSWORD, SEEDED_DEVICE_ID


class _Routes(list):
    """WebSocket routes plus the messages the browser sent to the fake server."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[dict] = []


def _open_connected_page(page, app_server_with_device):
    routes = _Routes()

    def handle_ws(ws) -> None:
        server = ws.connect_to_server()

        def forward_client(message) -> None:
            routes.messages.append(json.loads(message))
            server.send(message)

        ws.on_message(forward_client)

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


def _messages(routes, message_type: str) -> list[dict]:
    return [message for message in routes.messages if message.get("type") == message_type]


def _wait_for(page, callback, predicate=lambda value: bool(value)):
    for _ in range(100):
        value = callback()
        if predicate(value):
            return value
        page.wait_for_timeout(50)
    raise AssertionError("timed out waiting for browser result")


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
    assert _wait_for(page, lambda: len(_messages(routes, "term_claim"))) == 1
    _send(routes, {"type": "term_control", "device_id": SEEDED_DEVICE_ID, "run_id": "mine", "yours": True, "rev": 3})
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
    assert _wait_for(page, lambda: len(_messages(routes, "term_claim"))) == 1
    expect(box.locator(".terminal-strip")).to_have_text("Running. Click in this window to type.")
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


@pytest.mark.browser
def test_own_terminal_claims_before_run_reply(page, app_server_with_device):
    card, routes = _open_connected_page(page, app_server_with_device)
    pending = []

    def run_route(route) -> None:
        if pending:
            route.fulfill(status=200, content_type="application/json", body='{"ok": true, "run_id": "after-reply"}')
        else:
            pending.append(route)

    page.route("**/api/ssh/run", run_route)
    card.locator("[data-custom]").fill("printf mine")
    with page.expect_request("**/api/ssh/run") as request_info:
        card.locator("[data-run]").click()
    cmd_id = request_info.value.post_data_json["cmd_id"]
    _start(routes, run_id="before-reply", cmd_id=cmd_id)
    assert _wait_for(page, lambda: len(_messages(routes, "term_claim"))) == 1
    assert _messages(routes, "term_claim")[0]["run_id"] == "before-reply"


@pytest.mark.browser
def test_terminal_control_gates_typing_and_watcher_claims(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    box = _box(page)
    box.locator(".terminal-frame").click()
    box.locator("textarea").press_sequentially("before")
    assert not _messages(routes, "term_input")
    _send(routes, {"type": "term_control", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "yours": True, "rev": 7})
    box.locator("textarea").press_sequentially("after")
    assert _wait_for(page, lambda: len(_messages(routes, "term_input"))) == 5
    assert "".join(message["data"] for message in _messages(routes, "term_input")) == "after"
    assert all(message["rev"] == 7 for message in _messages(routes, "term_input"))
    _send(routes, {"type": "term_control", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "yours": False, "rev": 8})
    expect(box.locator(".terminal-strip")).to_have_text("Typing is in another tab. Click here to type.")
    box.locator("textarea").press_sequentially("blocked")
    assert len(_messages(routes, "term_input")) == 5
    box.locator(".terminal-strip").click()
    assert _wait_for(page, lambda: len(_messages(routes, "term_claim"))) == 2
    _send(routes, {"type": "term_control", "device_id": SEEDED_DEVICE_ID, "run_id": "other-run", "yours": True, "rev": 9})
    assert box.get_attribute("data-yours") == "false"
    assert box.get_attribute("data-rev") == "8"


@pytest.mark.browser
def test_terminal_paste_chunks_without_splitting_surrogate_pairs(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    _send(routes, {"type": "term_control", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "yours": True, "rev": 4})
    text = "a" * 16383 + "😀" + "b" * 23615
    page.evaluate("args => LIVE_TERMINALS.get(args[0]).terminal.paste(args[1])", [SEEDED_DEVICE_ID, text])
    assert _wait_for(page, lambda: len(_messages(routes, "term_input"))) == 3
    pieces = [message["data"] for message in _messages(routes, "term_input")]
    assert "".join(pieces) == text
    assert all(len(piece) <= 16384 for piece in pieces)
    assert all(not ("\ud800" <= piece[-1] <= "\udbff") for piece in pieces if piece)


@pytest.mark.browser
def test_terminal_ctrl_c_copy_or_send_and_input_full_message(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=app_server_with_device)
    _start(routes)
    box = _box(page)
    _send(routes, {"type": "term_control", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "yours": True, "rev": 2})
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": "copy this\r\n"})
    page.evaluate("id => LIVE_TERMINALS.get(id).terminal.selectAll()", SEEDED_DEVICE_ID)
    box.locator("textarea").press("Control+c")
    assert "copy this" in _wait_for(page, lambda: page.evaluate("() => navigator.clipboard.readText()"))
    assert not _messages(routes, "term_input")
    page.evaluate("id => LIVE_TERMINALS.get(id).terminal.clearSelection()", SEEDED_DEVICE_ID)
    box.locator("textarea").press("Control+c")
    assert _wait_for(page, lambda: len(_messages(routes, "term_input"))) == 1
    assert _messages(routes, "term_input")[0]["data"] == "\x03"
    _send(routes, {"type": "term_input_full", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one"})
    expect(box.locator(".terminal-strip")).to_have_text("Typing is arriving faster than the device accepts it.")
    page.wait_for_timeout(4100)
    expect(box.locator(".terminal-strip")).to_have_text("Running. Your typing goes to this command.")


@pytest.mark.browser
def test_terminal_resize_is_controller_only_and_debounced(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    _send(routes, {"type": "term_control", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "yours": True, "rev": 11})
    for size in ({"width": 1100, "height": 700}, {"width": 1000, "height": 650}, {"width": 900, "height": 600}):
        page.set_viewport_size(size)
    assert _wait_for(page, lambda: len(_messages(routes, "term_resize"))) == 1
    resize = _messages(routes, "term_resize")[0]
    assert resize["rev"] == 11
    assert 20 <= resize["cols"] <= 500 and 5 <= resize["rows"] <= 200
    _send(routes, {"type": "term_control", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "yours": False, "rev": 12})
    page.set_viewport_size({"width": 850, "height": 580})
    page.wait_for_timeout(200)
    assert len(_messages(routes, "term_resize")) == 1


@pytest.mark.browser
def test_terminal_fast_keystrokes_arrive_in_order_and_stop_at_end(page, app_server_with_device, capsys):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    box = _box(page)
    _send(routes, {"type": "term_control", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "yours": True, "rev": 5})
    text = "x" * 200
    box.locator("textarea").press_sequentially(text)
    assert _wait_for(page, lambda: sum(len(message["data"]) for message in _messages(routes, "term_input"))) == 200
    messages = _messages(routes, "term_input")
    assert "".join(message["data"] for message in messages) == text
    print(f"extra terminal input: messages={len(messages)}, characters={sum(len(message['data']) for message in messages)}")
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one"})
    box.locator("textarea").press_sequentially("late")
    page.wait_for_timeout(100)
    assert sum(len(message["data"]) for message in _messages(routes, "term_input")) == 200


@pytest.mark.browser
def test_ctrl_v_pastes_the_clipboard_exactly_once(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=app_server_with_device)
    _start(routes)
    box = _box(page)
    _send(routes, {"type": "term_control", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "yours": True, "rev": 3})
    page.evaluate("() => navigator.clipboard.writeText('pasted once')")
    box.locator(".terminal-frame").click()
    box.locator("textarea").press("Control+v")
    assert _wait_for(page, lambda: "".join(m["data"] for m in _messages(routes, "term_input"))) == "pasted once"
    page.wait_for_timeout(300)
    assert "".join(m["data"] for m in _messages(routes, "term_input")) == "pasted once"


@pytest.mark.browser
def test_own_terminal_claims_after_run_reply_and_other_runs_wait_for_click(page, app_server_with_device):
    card, routes = _open_connected_page(page, app_server_with_device)
    page.route("**/api/ssh/run", lambda route: route.fulfill(status=200, content_type="application/json", body='{"ok": true, "run_id": "after-reply"}'))
    card.locator("[data-custom]").fill("printf mine")
    with page.expect_request("**/api/ssh/run") as request_info:
        card.locator("[data-run]").click()
    page.locator("#saveSkip").click()
    _start(routes, run_id="after-reply", cmd_id=request_info.value.post_data_json["cmd_id"])
    assert _wait_for(page, lambda: len(_messages(routes, "term_claim"))) == 1
    assert _messages(routes, "term_claim")[0]["run_id"] == "after-reply"
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "after-reply"})

    _start(routes, run_id="elsewhere", cmd_id="c-from-another-tab")
    page.wait_for_timeout(300)
    assert len(_messages(routes, "term_claim")) == 1
    _box(page, run_id="elsewhere").locator(".terminal-frame").click()
    assert _wait_for(page, lambda: len(_messages(routes, "term_claim"))) == 2
    assert _messages(routes, "term_claim")[1]["run_id"] == "elsewhere"


@pytest.mark.browser
def test_hidden_controller_tab_resizes_when_shown(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    _send(routes, {"type": "term_control", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "yours": True, "rev": 6})
    _start(routes, device_id="other-device", run_id="other-run")
    page.locator('[data-tab="other-device"]').click()
    page.wait_for_timeout(300)
    before = len(_messages(routes, "term_resize"))
    page.set_viewport_size({"width": 900, "height": 560})
    page.wait_for_timeout(400)
    assert len(_messages(routes, "term_resize")) == before
    page.locator(f'[data-tab="{SEEDED_DEVICE_ID}"]').click()
    assert _wait_for(page, lambda: len(_messages(routes, "term_resize"))) == before + 1
    resize = _messages(routes, "term_resize")[-1]
    assert resize["run_id"] == "run-one" and resize["rev"] == 6
