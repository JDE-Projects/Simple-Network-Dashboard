"""Tests for _WSManager's per-socket device selection.

Covers the selection seams used by the metrics loop (selected_device_ids),
the websocket select_device handler (set_selected_device), disconnect
cleanup (drop), and the delete_device prune helper
(unselect_device_everywhere). These are unit-level tests against a fresh
_WSManager instance; no real WebSocket connections are involved.
"""

from main import _WSManager


def _fresh_manager() -> _WSManager:
    return _WSManager()


def test_two_sockets_selecting_different_devices_union_both():
    mgr = _fresh_manager()
    ws_a, ws_b = object(), object()

    mgr.set_selected_device(ws_a, "dev_a")
    mgr.set_selected_device(ws_b, "dev_b")

    assert mgr.selected_device_ids() == {"dev_a", "dev_b"}


def test_two_sockets_selecting_same_device_union_has_it_once():
    mgr = _fresh_manager()
    ws_a, ws_b = object(), object()

    mgr.set_selected_device(ws_a, "dev_a")
    mgr.set_selected_device(ws_b, "dev_a")

    assert mgr.selected_device_ids() == {"dev_a"}


def test_socket_changing_selection_updates_union():
    mgr = _fresh_manager()
    ws_a = object()

    mgr.set_selected_device(ws_a, "dev_a")
    mgr.set_selected_device(ws_a, "dev_b")

    assert mgr.selected_device_ids() == {"dev_b"}


def test_clearing_selection_with_empty_id_removes_entry():
    mgr = _fresh_manager()
    ws_a, ws_b = object(), object()

    mgr.set_selected_device(ws_a, "dev_a")
    mgr.set_selected_device(ws_b, "dev_b")
    mgr.set_selected_device(ws_a, None)

    assert mgr.selected_device_ids() == {"dev_b"}

    mgr.set_selected_device(ws_b, "")
    assert mgr.selected_device_ids() == set()


def test_drop_removes_that_sockets_selection():
    mgr = _fresh_manager()
    ws_a, ws_b = object(), object()

    mgr.set_selected_device(ws_a, "dev_a")
    mgr.set_selected_device(ws_b, "dev_b")

    mgr.drop(ws_a)

    assert mgr.selected_device_ids() == {"dev_b"}


def test_unselect_device_everywhere_removes_it_from_all_sockets():
    mgr = _fresh_manager()
    ws_a, ws_b, ws_c = object(), object(), object()

    mgr.set_selected_device(ws_a, "dev_a")
    mgr.set_selected_device(ws_b, "dev_a")
    mgr.set_selected_device(ws_c, "dev_b")

    mgr.unselect_device_everywhere("dev_a")

    assert mgr.selected_device_ids() == {"dev_b"}
