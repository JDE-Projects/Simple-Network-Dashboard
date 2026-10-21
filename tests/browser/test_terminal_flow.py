"""Browser acknowledgement behavior for completed xterm writes."""

import pytest

from tests.browser.conftest import SEEDED_DEVICE_ID
from tests.browser.test_live_terminal import _messages, _open_connected_page, _send, _start, _wait_for
from tests.browser.test_terminal_refresh import _entry, _open_recovery_page


@pytest.mark.browser
def test_terminal_acknowledges_completed_writes_with_a_trailing_latest_ack(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": "one\r\n"})
    assert _wait_for(page, lambda: _messages(routes, "term_ack"))[0]["seq"] == 1
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 2, "data": "two\r\n"})
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 3, "data": "three\r\n"})
    acks = _wait_for(page, lambda: _messages(routes, "term_ack"), lambda values: len(values) == 2)
    assert [message["seq"] for message in acks] == [1, 3]


@pytest.mark.browser
def test_hidden_page_acknowledges_on_receipt_before_drawing(page, app_server_with_device):
    _, routes = _open_connected_page(page, app_server_with_device)
    _start(routes)
    _wait_for(page, lambda: page.evaluate("id => LIVE_TERMINALS.has(id)", SEEDED_DEVICE_ID))
    # Stand in for a background tab: the page reports hidden and drawing never
    # finishes, as when the browser throttles a hidden tab's timers.
    page.evaluate("""id => {
        Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
        LIVE_TERMINALS.get(id).terminal.write = () => {};
    }""", SEEDED_DEVICE_ID)
    _send(routes, {"type": "term_data", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 1, "data": "hidden\r\n"})
    assert _wait_for(page, lambda: _messages(routes, "term_ack"))[0]["seq"] == 1


@pytest.mark.browser
def test_rebuilt_terminal_acknowledges_the_init_snapshot(page, app_server_with_device):
    routes = _open_recovery_page(page, app_server_with_device, [[_entry(seq=5)]])
    ack = _wait_for(page, lambda: _messages(routes, "term_ack"))[0]
    assert ack == {"type": "term_ack", "device_id": SEEDED_DEVICE_ID, "run_id": "run-one", "seq": 5}
