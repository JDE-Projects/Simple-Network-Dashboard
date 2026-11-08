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

from simple_network_dashboard import debug_log, main, runtime_state


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
    monkeypatch.setattr(runtime_state, "_devices_cache", _fake_devices())
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
    monkeypatch.setattr(runtime_state, "_devices_cache", _fake_devices())
    metrics_cache = {}
    monkeypatch.setattr(main, "_metrics_cache", metrics_cache)
    _select(monkeypatch, {"dev_a", "dev_b"})

    async def fake_fetch_metrics(host, port, prev=None):
        if host == "10.0.0.4":
            raise RuntimeError("raw-fetch-error-marker")
        return {"cpu_percent": 5.0}
    monkeypatch.setattr(main, "fetch_metrics", fake_fetch_metrics)

    broadcasts, broadcast = _recorder()
    monkeypatch.setattr(main.ws_mgr, "broadcast", broadcast)

    debug_lines = []
    monkeypatch.setattr(debug_log, "_debug_write", lambda text, *a, **k: debug_lines.append(text))

    asyncio.run(main._poll_once())  # must not raise

    by_id = {msg["device_id"]: msg for msg in broadcasts}
    assert by_id["dev_b"]["data"] == {"cpu_percent": 5.0}
    assert by_id["dev_a"]["data"] == {"error": "failed (RuntimeError)"}
    assert metrics_cache["dev_a"] == {"error": "failed (RuntimeError)"}
    assert debug_lines
    assert not any("raw-fetch-error-marker" in line for line in debug_lines)
    assert metrics_cache["dev_b"] == {"cpu_percent": 5.0}


def test_poll_once_broadcasts_fast_result_before_slow_fetch_finishes(monkeypatch):
    monkeypatch.setattr(runtime_state, "_devices_cache", _fake_devices())
    monkeypatch.setattr(main, "_metrics_cache", {})
    _select(monkeypatch, {"dev_a", "dev_b"})

    slow_started = asyncio.Event()
    release_slow = asyncio.Event()
    slow_finished = False
    fast_broadcast = asyncio.Event()

    async def fake_fetch_metrics(host, port, prev=None):
        nonlocal slow_finished
        if host == "10.0.0.4":
            slow_started.set()
            await release_slow.wait()
            slow_finished = True
        return {"cpu_percent": 5.0}
    monkeypatch.setattr(main, "fetch_metrics", fake_fetch_metrics)

    broadcasts, broadcast = _recorder()

    async def record_broadcast(message):
        await broadcast(message)
        if message["device_id"] == "dev_b":
            fast_broadcast.set()
    monkeypatch.setattr(main.ws_mgr, "broadcast", record_broadcast)

    async def run_test():
        task = asyncio.create_task(main._poll_once())
        await slow_started.wait()
        await fast_broadcast.wait()
        assert not slow_finished
        release_slow.set()
        await task

    asyncio.run(run_test())

    assert [message["device_id"] for message in broadcasts] == ["dev_b", "dev_a"]


def test_poll_once_limits_concurrent_fetches(monkeypatch):
    monkeypatch.setattr(runtime_state, "_devices_cache", _fake_devices())
    monkeypatch.setattr(main, "_metrics_cache", {})
    _select(monkeypatch, {"dev_a", "dev_b"})
    monkeypatch.setattr(main, "METRICS_MAX_CONCURRENT_FETCHES", 1)

    active = 0
    max_active = 0

    async def fake_fetch_metrics(host, port, prev=None):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return {"cpu_percent": 5.0}
    monkeypatch.setattr(main, "fetch_metrics", fake_fetch_metrics)

    broadcasts, broadcast = _recorder()
    monkeypatch.setattr(main.ws_mgr, "broadcast", broadcast)

    asyncio.run(main._poll_once())

    assert max_active == 1
    assert len(broadcasts) == 2


def test_poll_once_does_nothing_when_no_devices_selected(monkeypatch):
    monkeypatch.setattr(runtime_state, "_devices_cache", _fake_devices())
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
    monkeypatch.setattr(runtime_state, "_devices_cache", _fake_devices())
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


def test_metrics_loop_survives_failed_cycles_and_logs_once_per_streak(monkeypatch, capsys):
    """An unexpected cycle failure is logged once, the loop keeps going, and
    the next good cycle still reaches browsers and logs recovery."""
    monkeypatch.setattr(runtime_state, "_devices_cache", _fake_devices())
    monkeypatch.setattr(main, "_metrics_cache", {})
    monkeypatch.setattr(main, "METRICS_INTERVAL", 0)
    _select(monkeypatch, {"dev_a"})

    async def fake_fetch_metrics(host, port, prev=None):
        return {"cpu_percent": 5.0}
    monkeypatch.setattr(main, "fetch_metrics", fake_fetch_metrics)

    broadcasts, broadcast = _recorder()
    monkeypatch.setattr(main.ws_mgr, "broadcast", broadcast)

    real_poll_once = main._poll_once
    calls = []

    async def flaky_poll_once():
        calls.append(None)
        if len(calls) <= 2:
            raise TypeError("unhashable type: 'list'")
        if len(calls) == 3:
            return await real_poll_once()
        raise asyncio.CancelledError
    monkeypatch.setattr(main, "_poll_once", flaky_poll_once)

    async def run():
        try:
            await main._metrics_loop()
        except asyncio.CancelledError:
            pass
    asyncio.run(run())

    assert len(calls) == 4
    assert [b["device_id"] for b in broadcasts] == ["dev_a"]
    out = capsys.readouterr().out
    assert out.count("metrics polling cycle failed") == 1
    assert "TypeError: unhashable type: 'list'" in out
    assert out.count("polling recovered") == 1


def test_stalled_dns_lookup_does_not_hold_up_a_healthy_device(monkeypatch):
    """With the real fetch_metrics, a device whose name lookup never finishes
    times out within the deadline, and the healthy device is broadcast on
    this cycle and the next."""
    import functools

    import httpx

    from simple_network_dashboard import metrics_poller

    devices = [
        {"id": "dev_stuck", "host": "stuckhost", "metrics_port": 9100},
        {"id": "dev_ok", "host": "10.0.0.5", "metrics_port": 9100},
    ]
    monkeypatch.setattr(runtime_state, "_devices_cache", devices)
    monkeypatch.setattr(main, "_metrics_cache", {})
    monkeypatch.setattr(metrics_poller, "METRICS_FETCH_DEADLINE", 0.1)
    _select(monkeypatch, {"dev_stuck", "dev_ok"})

    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="node_load1 0.5\n"))
    monkeypatch.setattr(httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=transport))

    async def fake_getaddrinfo(self, host, port, *args, **kwargs):
        await asyncio.Event().wait()  # never completes
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)

    broadcasts, broadcast = _recorder()
    monkeypatch.setattr(main.ws_mgr, "broadcast", broadcast)

    async def two_cycles():
        await asyncio.wait_for(main._poll_once(), 2)
        await asyncio.wait_for(main._poll_once(), 2)
    asyncio.run(two_cycles())

    ok = [b for b in broadcasts if b["device_id"] == "dev_ok"]
    stuck = [b for b in broadcasts if b["device_id"] == "dev_stuck"]
    assert len(ok) == 2 and all("error" not in b["data"] for b in ok)
    assert [b["data"] for b in stuck] == [{"error": "dns timeout"}] * 2
