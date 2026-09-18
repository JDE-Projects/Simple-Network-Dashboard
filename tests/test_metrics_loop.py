"""Tests for main._poll_once, the single-cycle metrics polling helper.

Covers the two gaps not exercised by test_metrics_fetch.py (per-fetch
resolution/HTTP behavior) or test_socket_selection.py (selection bookkeeping
at the _WSManager level): that one poll cycle fetches every selected device
concurrently, that a single failed fetch is isolated from the others, that
an idle cycle (no selections) does nothing, and that a selected device
missing from the devices cache is skipped without error.

fetch_metrics is monkeypatched to an async fake so these tests never touch
DNS or HTTP; ws_mgr.broadcast is monkeypatched to a recorder so tests can
inspect exactly what was sent without a real WebSocket.
"""

import asyncio

import main


def _fake_devices():
    return [
        {"id": "dev_a", "host": "10.0.0.4", "metrics_port": 9100},
        {"id": "dev_b", "host": "10.0.0.5", "metrics_port": 9100},
    ]


def _select(monkeypatch, device_ids):
    """Make ws_mgr report exactly `device_ids` as selected, with at least
    one connection present (an empty _connections list short-circuits
    _poll_once before selected_device_ids() is even consulted)."""
    monkeypatch.setattr(main.ws_mgr, "_connections", [object()])
    monkeypatch.setattr(main.ws_mgr, "selected_device_ids", lambda: set(device_ids))


def _recorder():
    calls = []

    async def broadcast(msg):
        calls.append(msg)
    return calls, broadcast


def test_poll_once_fetches_both_selected_devices_concurrently(monkeypatch):
    monkeypatch.setattr(main, "_devices_cache", _fake_devices())
    monkeypatch.setattr(main, "_metrics_cache", {})
    _select(monkeypatch, {"dev_a", "dev_b"})

    fetch_calls = []

    async def fake_fetch_metrics(host, port, prev=None):
        fetch_calls.append((host, port))
        return {"cpu_percent": 5.0, "_raw": "internal"}
    monkeypatch.setattr(main, "fetch_metrics", fake_fetch_metrics)

    broadcasts, broadcast = _recorder()
    monkeypatch.setattr(main.ws_mgr, "broadcast", broadcast)

    asyncio.run(main._poll_once())

    assert set(fetch_calls) == {("10.0.0.4", 9100), ("10.0.0.5", 9100)}
    assert len(broadcasts) == 2
    seen_ids = set()
    for msg in broadcasts:
        assert msg["type"] == "metrics"
        assert msg["data"] == {"cpu_percent": 5.0}  # underscore key stripped
        seen_ids.add(msg["device_id"])
    assert seen_ids == {"dev_a", "dev_b"}


def test_poll_once_isolates_a_single_failed_fetch(monkeypatch):
    monkeypatch.setattr(main, "_devices_cache", _fake_devices())
    metrics_cache = {}
    monkeypatch.setattr(main, "_metrics_cache", metrics_cache)
    _select(monkeypatch, {"dev_a", "dev_b"})

    async def fake_fetch_metrics(host, port, prev=None):
        if host == "10.0.0.4":
            raise RuntimeError("boom")
        return {"cpu_percent": 5.0}
    monkeypatch.setattr(main, "fetch_metrics", fake_fetch_metrics)

    broadcasts, broadcast = _recorder()
    monkeypatch.setattr(main.ws_mgr, "broadcast", broadcast)

    monkeypatch.setattr(main, "_debug_write", lambda *a, **k: None)

    asyncio.run(main._poll_once())  # must not raise

    by_id = {msg["device_id"]: msg for msg in broadcasts}
    assert by_id["dev_b"]["data"] == {"cpu_percent": 5.0}
    assert by_id["dev_a"]["data"] == {"error": "boom"}
    assert metrics_cache["dev_a"] == {"error": "boom"}
    assert metrics_cache["dev_b"] == {"cpu_percent": 5.0}


def test_poll_once_does_nothing_when_no_devices_selected(monkeypatch):
    monkeypatch.setattr(main, "_devices_cache", _fake_devices())
    monkeypatch.setattr(main, "_metrics_cache", {})
    _select(monkeypatch, set())

    async def fake_fetch_metrics(host, port, prev=None):
        raise AssertionError("fetch_metrics should not have been called")
    monkeypatch.setattr(main, "fetch_metrics", fake_fetch_metrics)

    broadcasts, broadcast = _recorder()
    monkeypatch.setattr(main.ws_mgr, "broadcast", broadcast)

    asyncio.run(main._poll_once())

    assert broadcasts == []


def test_poll_once_skips_selected_id_missing_from_devices_cache(monkeypatch):
    monkeypatch.setattr(main, "_devices_cache", _fake_devices())
    monkeypatch.setattr(main, "_metrics_cache", {})
    _select(monkeypatch, {"dev_a", "dev_missing"})

    fetch_calls = []

    async def fake_fetch_metrics(host, port, prev=None):
        fetch_calls.append((host, port))
        return {"cpu_percent": 5.0}
    monkeypatch.setattr(main, "fetch_metrics", fake_fetch_metrics)

    broadcasts, broadcast = _recorder()
    monkeypatch.setattr(main.ws_mgr, "broadcast", broadcast)

    asyncio.run(main._poll_once())  # must not raise

    assert fetch_calls == [("10.0.0.4", 9100)]
    assert len(broadcasts) == 1
    assert broadcasts[0]["device_id"] == "dev_a"
