"""Browser coverage for folding a completed xterm box into console rows."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import SEEDED_DEVICE_ID
from tests.browser.test_live_terminal import _box, _open_connected_page, _send, _start


def _console(page):
    return page.locator(f'[data-console="{SEEDED_DEVICE_ID}"]')


def _messages(page) -> list[str]:
    return _console(page).locator(".ln .msg").all_text_contents()


def _tags(page) -> list[str]:
    return _console(page).locator(".ln .tag").all_text_contents()


def _data(page, routes, seq: int, data: str) -> None:
    """Send a batch and wait until the terminal has received and drawn it, so a
    clock change afterwards cannot land on its line feeds."""
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": seq, "data": data})
    page.wait_for_function("([id, seq]) => (LIVE_TERMINALS.get(id) || {}).lastSeq >= seq", arg=[SEEDED_DEVICE_ID, seq])
    page.evaluate("id => new Promise(done => LIVE_TERMINALS.get(id).terminal.write('', done))", SEEDED_DEVICE_ID)


def _time(page, value: str) -> str:
    page.clock.set_fixed_time(value)
    return page.evaluate("() => new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'})")


@pytest.mark.browser
def test_fold_keeps_line_feed_times_and_scrollback_output(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes, rows=3)
    times = []
    for index, value in enumerate(("2024-01-01T12:00:01", "2024-01-01T12:00:02", "2024-01-01T12:00:03"), 1):
        times.append(_time(page, value))
        _data(page, routes, index, f"line {index}\r\n")
    _time(page, "2024-01-01T12:00:09")
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one"})
    expect(_box(page)).to_have_count(0)
    assert _messages(page) == ["line 1", "line 2", "line 3"]
    assert _tags(page) == times


@pytest.mark.browser
def test_fold_keeps_echoed_answer_and_wrapped_output(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes, cols=80, rows=2)
    payload = "Continue? [Y/n] y\r\n" + "".join(f"row {index}\r\n" for index in range(8)) + "x" * 200 + "\r\n"
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": payload})
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one"})
    expect(_box(page)).to_have_count(0)
    assert _messages(page) == ["Continue? [Y/n] y", *[f"row {index}" for index in range(8)], "x" * 200]


@pytest.mark.browser
def test_fold_progress_redraw_uses_final_text(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": "10%\r50%\r100%\r\n"})
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one"})
    expect(_box(page)).to_have_count(0)
    assert _messages(page) == ["100%"]


@pytest.mark.browser
def test_fold_marks_closed_full_screen_view_without_its_contents(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    _data(page, routes, 1, "before\r\n")
    closed_time = _time(page, "2024-01-01T12:00:04")
    _data(page, routes, 2, "\x1b[?1049hsecret screen\x1b[?1049lafter\r\n")
    _time(page, "2024-01-01T12:00:09")
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one"})
    expect(_box(page)).to_have_count(0)
    assert _messages(page) == ["before", "(full-screen view closed)", "after"]
    assert _tags(page)[1] == closed_time
    assert "secret screen" not in _console(page).text_content()


@pytest.mark.browser
def test_fold_discards_closure_when_full_screen_restarts_immediately(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    # nano answering a resize: it moves the cursor, leaves the full-screen
    # view and re-enters it, twice in one batch (captured from a real device).
    bounce = "\x1b[28;1H\x1b[?1049l\x1b[23;0;0t\r\x1b[?1l\x1b>\x1b[?1049h\x1b[22;0;0t\x1b[1;27r\x1b[H\x1b[2J  GNU nano"
    _data(page, routes, 1, "\x1b[?1049h\x1b[H\x1b[2J  GNU nano")
    _data(page, routes, 2, bounce + bounce)
    _data(page, routes, 3, "\x1b[28;1H\x1b[?1049l")
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one"})
    expect(_box(page)).to_have_count(0)
    assert _messages(page) == ["(full-screen view closed)"]


@pytest.mark.browser
def test_fold_keeps_closures_when_normal_output_separates_full_screen_sessions(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    _data(page, routes, 1, "\x1b[?1049h")
    _data(page, routes, 2, "\x1b[?1049l\x1b[23;0;0t\r\x1b[?1049h")
    _data(page, routes, 3, "\x1b[?1049lfirst session finished\r\n\x1b[?1049h")
    _data(page, routes, 4, "\x1b[?1049l")
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one"})
    expect(_box(page)).to_have_count(0)
    messages = _messages(page)
    assert messages.count("(full-screen view closed)") == 2
    assert "first session finished" in messages


@pytest.mark.browser
def test_fold_scrollback_trim_keeps_times_aligned_and_console_cap(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes, rows=5)
    first_time = _time(page, "2024-01-01T12:00:05")
    _data(page, routes, 1, "".join(f"old {index}\r\n" for index in range(4800)))
    second_time = _time(page, "2024-01-01T12:00:06")
    _data(page, routes, 2, "".join(f"new {index}\r\n" for index in range(400)))
    _time(page, "2024-01-01T12:00:09")
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one"})
    expect(_box(page)).to_have_count(0)
    messages = _messages(page)
    tags = _tags(page)
    assert len(messages) == 1500
    assert messages[0].startswith("old ") and tags[0] == first_time
    # The top 195 rows were trimmed from scrollback; times must still match
    # their lines on both sides of the switch from old to new output.
    boundary = messages.index("new 0")
    assert messages[boundary - 1] == "old 4799" and tags[boundary - 1] == first_time
    assert tags[boundary] == second_time
    assert messages[-1] == "new 399" and tags[-1] == second_time


@pytest.mark.browser
def test_fold_queues_app_messages_after_terminal_rows(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _send(routes, {"type": "ssh_log", "device_id": SEEDED_DEVICE_ID, "text": "before", "level": "out"})
    before_time = _tags(page)[0]
    _start(routes)
    terminal_time = _time(page, "2024-01-01T12:00:07")
    _data(page, routes, 1, "terminal\r\n")
    run_time = _time(page, "2024-01-01T12:00:08")
    _send(routes, {"type": "ssh_log", "device_id": SEEDED_DEVICE_ID, "text": "during run", "level": "warn"})
    expect(_console(page).locator(".ln", has_text="terminal")).to_have_count(0)
    page.wait_for_timeout(200)
    assert "during run" not in _messages(page)
    fold_time = _time(page, "2024-01-01T12:00:09")
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one"})
    _send(routes, {"type": "ssh_log", "device_id": SEEDED_DEVICE_ID, "text": "after end", "level": "ok"})
    expect(_box(page)).to_have_count(0)
    expect(_console(page).locator(".ln", has_text="after end")).to_have_count(1)
    assert _messages(page) == ["before", "terminal", "during run", "after end"]
    assert _tags(page) == [before_time, terminal_time, run_time, fold_time]


@pytest.mark.browser
def test_fold_on_disconnect_or_replacement_and_queue_limit(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes, run_id="first")
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "first", "seq": 1, "data": "first\r\n"})
    _send(routes, {"type": "ssh_status", "device_id": SEEDED_DEVICE_ID, "state": "idle"})
    expect(_box(page, run_id="first")).to_have_count(0)
    assert "first" in _messages(page)

    _start(routes, run_id="second")
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "second", "seq": 1, "data": "second\r\n"})
    _start(routes, run_id="third")
    expect(_box(page, run_id="second")).to_have_count(0)
    expect(_box(page, run_id="third")).to_have_count(1)
    for index in range(205):
        _send(routes, {"type": "ssh_log", "device_id": SEEDED_DEVICE_ID, "text": f"queued {index}", "level": "out"})
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "third"})
    expect(_box(page, run_id="third")).to_have_count(0)
    messages = _messages(page)
    note = "Some app messages during this command were not kept."
    assert note in messages
    assert messages[-200:] == [f"queued {index}" for index in range(5, 205)]


@pytest.mark.browser
def test_fold_disposes_box_ignores_late_data_and_has_no_console_errors(page, app_server_with_device):
    errors: list[str] = []
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: errors.append(str(error)))
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": "kept\r\n\r\n"})
    _send(routes, {"type": "term_end", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one"})
    expect(_box(page)).to_have_count(0)
    expect(_console(page).locator(".xterm")).to_have_count(0)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 2, "data": "late"})
    assert _messages(page) == ["kept"]
    assert not errors
