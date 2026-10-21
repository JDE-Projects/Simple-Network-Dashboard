"""Browser coverage for pager guidance in the live terminal strip."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import SEEDED_DEVICE_ID
from tests.browser.test_live_terminal import _box, _open_connected_page, _send, _start
from tests.browser.test_terminal_refresh import _entry, _open_recovery_page


PAGER_HINT = "Paged view? Click here, then q closes it"
FOCUSED_PAGER_HINT = "Paged view? q closes it · Ctrl+C stops the command"


@pytest.mark.browser
@pytest.mark.parametrize(
    "prompt",
    [
        "lines 1-23/57 50%",
        "(END)",
        ":",
        "--More--(40%)",
        "Manual page ls(1) line 12 (press h for help or q to quit)",
    ],
)
def test_pager_prompts_show_guidance(page, app_server_with_device, prompt):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    box = _box(page)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": prompt})
    expect(box.locator(".terminal-strip")).to_have_text(PAGER_HINT)


@pytest.mark.browser
@pytest.mark.parametrize("output", ["total: 3 lines", "foo:"])
def test_non_pager_text_does_not_show_guidance(page, app_server_with_device, output):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    box = _box(page)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": output})
    expect(box.locator(".terminal-strip")).to_have_text("Running. Click in this window to type.")


@pytest.mark.browser
def test_pager_guidance_clears_when_output_moves_to_another_line(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    box = _box(page)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": "(END)"})
    expect(box.locator(".terminal-strip")).to_have_text(PAGER_HINT)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 2, "data": "\r\nfinished"})
    expect(box.locator(".terminal-strip")).to_have_text("Running. Click in this window to type.")


@pytest.mark.browser
def test_other_tab_and_redraw_messages_override_pager_guidance(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    box = _box(page)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": "(END)"})
    expect(box.locator(".terminal-strip")).to_have_text(PAGER_HINT)
    _send(routes, {"type": "term_control", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "yours": False, "rev": 1})
    expect(box.locator(".terminal-strip")).to_have_text("Typing is in another tab. Click here to type.")
    page.evaluate("""id => {
      const live = LIVE_TERMINALS.get(id);
      live.redrawWaiting = true;
      updateTerminalStrip(live);
    }""", SEEDED_DEVICE_ID)
    expect(box.locator(".terminal-strip")).to_have_text("Waiting for the program to redraw. Press a key in the window if it stays blank.")


@pytest.mark.browser
def test_focused_controller_gets_pager_shortcut_guidance(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    box = _box(page)
    _send(routes, {"type": "term_control", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "yours": True, "rev": 1})
    box.locator(".terminal-frame").click()
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": "(END)"})
    expect(box.locator(".terminal-strip")).to_have_text(FOCUSED_PAGER_HINT)


@pytest.mark.browser
def test_recovery_snapshot_ending_at_pager_prompt_shows_guidance(page, app_server_with_device):
    routes = _open_recovery_page(page, app_server_with_device, [[_entry(partial="(END)")]])
    box = _box(page)
    expect(box.locator(".terminal-strip")).to_have_text(PAGER_HINT)
    assert routes
