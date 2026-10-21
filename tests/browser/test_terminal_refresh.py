"""Browser coverage for restoring and exporting a running terminal."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import DASHBOARD_PASSWORD, SEEDED_DEVICE_ID
from tests.browser.test_live_terminal import _Routes, _box, _messages, _send, _wait_for


def _open_recovery_page(page, app_server_with_device, snapshots):
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
                snapshot = routes.snapshot
                msg["ssh_connected"] = [SEEDED_DEVICE_ID]
                msg["ssh_running"] = [SEEDED_DEVICE_ID] if snapshot else []
                msg["ssh_terminals"] = snapshot
                message = json.dumps(msg)
            ws.send(message)

        server.on_message(forward)
        routes.append(ws)

    routes.snapshot = snapshots[0]
    page.route_web_socket(re.compile(r"/ws"), handle_ws)
    page.goto(f"{app_server_with_device}/login")
    page.fill("#password", DASHBOARD_PASSWORD)
    page.click("#submit")
    page.wait_for_url(f"{app_server_with_device}/")
    # The page has applied its init once the card shows the connection.
    expect(page.locator(f'[data-card="{SEEDED_DEVICE_ID}"]')).to_have_class(re.compile(r"\bssh-on\b"))
    routes.snapshots = snapshots
    return routes


def _entry(**changes):
    entry = {
        "device_id": SEEDED_DEVICE_ID,
        "run_id": "run-one",
        "cols": 80,
        "rows": 24,
        "lines": ["one", "two", "three"],
        "partial": "Continue? [Y/n] ",
        "fullscreen": False,
        "seq": 5,
    }
    entry.update(changes)
    return entry


def _console(page):
    return page.locator(f'[data-console="{SEEDED_DEVICE_ID}"]')


@pytest.mark.browser
def test_reload_mid_command_rebuilds_watcher_and_filters_duplicate_batches(page, app_server_with_device):
    routes = _open_recovery_page(page, app_server_with_device, [[_entry()], [_entry()]])
    page.reload()
    box = _box(page)
    expect(box).to_have_count(1)
    assert _console(page).locator(".ln .msg").all_text_contents() == ["$ Command", "one", "two", "three"]
    assert _console(page).locator(".ln .tag").all_text_contents() == ["", "", "", ""]
    expect(box.locator(".xterm-rows")).to_contain_text("Continue? [Y/n]")
    expect(box.locator(".terminal-strip")).to_have_text("Running. Click in this window to type.")
    assert not _messages(routes, "term_claim")
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 4, "data": "old-four"})
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 5, "data": "old-five"})
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 6, "data": "new-six"})
    expect(box.locator(".xterm-rows")).to_contain_text("new-six")
    expect(box.locator(".xterm-rows")).not_to_contain_text("old-four")
    expect(box.locator(".xterm-rows")).not_to_contain_text("old-five")
    box.locator(".terminal-frame").click()
    assert _wait_for(page, lambda: len(_messages(routes, "term_claim"))) == 1
    _send(routes, {"type": "term_control", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "yours": True, "rev": 9})
    page.keyboard.type("y")
    page.keyboard.press("Enter")
    assert _wait_for(page, lambda: "".join(message["data"] for message in _messages(routes, "term_input"))) == "y\r"
    assert all(message["rev"] == 9 for message in _messages(routes, "term_input"))


@pytest.mark.browser
def test_reconnect_keeps_current_box_but_resets_control(page, app_server_with_device):
    routes = _open_recovery_page(page, app_server_with_device, [[], [_entry(lines=[], partial="", seq=1)]])
    _send(routes, {"type": "term_start", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "cmd_id": "other", "cols": 80, "rows": 24})
    box = _box(page)
    expect(box).to_have_count(1)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": "kept\r\n"})
    expect(box.locator(".xterm-rows")).to_contain_text("kept")
    box.locator(".terminal-frame").click()
    assert _wait_for(page, lambda: len(_messages(routes, "term_claim"))) == 1
    _send(routes, {"type": "term_control", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "yours": True, "rev": 3})
    expect(box).to_have_attribute("data-yours", "true")
    page.keyboard.type("a")
    assert _wait_for(page, lambda: "".join(message["data"] for message in _messages(routes, "term_input"))) == "a"
    box.evaluate("element => element.recoveryMarker = true")
    routes.snapshot = routes.snapshots[1]
    page.evaluate("() => ws.close()")
    assert _wait_for(page, lambda: len(routes), lambda value: value == 2)
    # The box kept keyboard focus, so it claims again on the new socket once the
    # new init is applied.
    assert _wait_for(page, lambda: len(_messages(routes, "term_claim")), lambda value: value == 2) == 2
    expect(box.locator(".xterm-rows")).to_contain_text("kept")
    assert box.evaluate("element => element.recoveryMarker") is True
    assert box.get_attribute("data-yours") is None
    assert box.get_attribute("data-rev") is None
    page.keyboard.type("blocked")
    page.wait_for_timeout(200)
    assert "".join(message["data"] for message in _messages(routes, "term_input")) == "a"
    _send(routes, {"type": "term_control", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "yours": True, "rev": 4})
    expect(box).to_have_attribute("data-yours", "true")
    page.keyboard.type("ok")
    assert _wait_for(page, lambda: "".join(message["data"] for message in _messages(routes, "term_input")), lambda value: value == "aok") == "aok"
    assert _messages(routes, "term_input")[-1]["rev"] == 4


@pytest.mark.browser
def test_reconnect_with_a_different_run_folds_the_old_box(page, app_server_with_device):
    routes = _open_recovery_page(page, app_server_with_device, [[], [_entry(run_id="run-two", lines=["new history"], partial="", seq=3)]])
    _send(routes, {"type": "term_start", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "cmd_id": "other", "cols": 80, "rows": 24})
    expect(_box(page)).to_have_count(1)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": "first run output\r\n"})
    expect(_box(page).locator(".xterm-rows")).to_contain_text("first run output")
    routes.snapshot = routes.snapshots[1]
    page.evaluate("() => ws.close()")
    assert _wait_for(page, lambda: len(routes), lambda value: value == 2)
    expect(_box(page, run_id="run-two")).to_have_count(1)
    expect(_box(page)).to_have_count(0)
    expect(_console(page).locator(".ln .msg")).to_have_text(["first run output", "$ Command", "new history"])


@pytest.mark.browser
def test_reconnect_rebuilds_missed_output_and_folds_ended_run(page, app_server_with_device):
    routes = _open_recovery_page(page, app_server_with_device, [[], [_entry(lines=["missed"], partial="partial", seq=2)], []])
    _send(routes, {"type": "term_start", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "cmd_id": "other", "cols": 80, "rows": 24})
    old_box = _box(page)
    expect(old_box).to_have_count(1)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": "old\r\n"})
    expect(old_box.locator(".xterm-rows")).to_contain_text("old")
    old_handle = old_box.element_handle()
    routes.snapshot = routes.snapshots[1]
    page.evaluate("() => ws.close()")
    assert _wait_for(page, lambda: len(routes), lambda value: value == 2)
    expect(_box(page).locator(".xterm-rows")).to_contain_text("partial")
    assert old_handle is not None and not old_handle.evaluate("element => element.isConnected")
    assert _console(page).locator(".ln .msg").all_text_contents() == ["missed"]
    assert _console(page).locator(".ln .tag").all_text_contents() == [""]
    expect(_box(page).locator(".xterm-rows")).to_contain_text("partial")
    routes.snapshot = routes.snapshots[2]
    page.evaluate("() => ws.close()")
    assert _wait_for(page, lambda: len(routes), lambda value: value == 3)
    expect(_box(page)).to_have_count(0)
    expect(_console(page).locator(".ln .msg")).to_have_text(["missed", "partial"])


@pytest.mark.browser
def test_full_screen_refresh_waits_for_redraw(page, app_server_with_device):
    entry = _entry(lines=["history"], partial="Name? ", fullscreen=True)
    routes = _open_recovery_page(page, app_server_with_device, [[entry]])
    box = _box(page)
    expect(box).to_have_class(re.compile(r"\bterminal-fullscreen\b"))
    expect(box.locator(".terminal-strip")).to_have_text("Waiting for the program to redraw. Press a key in the window if it stays blank.", timeout=3_000)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 6, "data": "\x1b[?1049lAnn\r\n"})
    expect(box.locator(".terminal-strip")).to_have_text("Running. Click in this window to type.")


@pytest.mark.browser
def test_fold_after_refresh_keeps_line_feed_time(page, app_server_with_device):
    routes = _open_recovery_page(page, app_server_with_device, [[_entry(lines=["history"], partial="Name? ")]])
    box = _box(page)
    expect(box).to_have_count(1)
    page.clock.set_fixed_time("2024-01-01T12:00:01")
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 6, "data": "Ann\r\n"})
    expect(box.locator(".xterm-rows")).to_contain_text("Name? Ann")
    line_time = page.evaluate("() => new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'})")
    page.clock.set_fixed_time("2024-01-01T12:00:07")
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one"})
    expect(_box(page)).to_have_count(0)
    messages = _console(page).locator(".ln .msg").all_text_contents()
    tags = _console(page).locator(".ln .tag").all_text_contents()
    assert messages == ["$ Command", "history", "Name? Ann"]
    assert tags[:2] == ["", ""]
    assert tags[2] == line_time


@pytest.mark.browser
def test_export_includes_running_terminal_without_changing_it(page, app_server_with_device, tmp_path):
    routes = _open_recovery_page(page, app_server_with_device, [[]])
    _send(routes, {"type": "ssh_log", "device_id": SEEDED_DEVICE_ID, "text": "finished", "level": "out"})
    _send(routes, {"type": "term_start", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "cmd_id": "other", "cols": 80, "rows": 24})
    box = _box(page)
    expect(box).to_have_count(1)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": "live line\r\n"})
    expect(box.locator(".xterm-rows")).to_contain_text("live line")
    with page.expect_download() as download_info:
        page.locator("#exportBtn").click()
    export_path = tmp_path / "running-terminal.txt"
    download_info.value.save_as(export_path)
    exported = export_path.read_text(encoding="utf-8")
    assert exported.splitlines()[-1].endswith("(command still running when exported)")
    assert "finished" in exported and "live line" in exported
    expect(box).to_have_count(1)


@pytest.mark.browser
def test_export_of_only_live_box_and_open_full_screen_view(page, app_server_with_device, tmp_path):
    routes = _open_recovery_page(page, app_server_with_device, [[]])
    _send(routes, {"type": "term_start", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "cmd_id": "other", "cols": 80, "rows": 24})
    expect(_box(page)).to_have_count(1)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": "\x1b[?1049hsecret"})
    expect(_box(page)).to_have_class(re.compile(r"\bterminal-fullscreen\b"))
    with page.expect_download() as download_info:
        page.locator("#exportBtn").click()
    export_path = tmp_path / "full-screen-terminal.txt"
    download_info.value.save_as(export_path)
    exported = export_path.read_text(encoding="utf-8")
    assert "Nothing to export." not in _console(page).text_content()
    assert "(full-screen view closed)" not in exported
    assert exported.splitlines()[-1].endswith("(command still running when exported)")
