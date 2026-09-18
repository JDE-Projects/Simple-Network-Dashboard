"""Tests for the per-device metrics error repeat control in main._poll_once.

A persistently-failing device would otherwise log an identical error line
every ~2 seconds. These tests drive main._poll_once directly (as
test_metrics_loop.py does), monkeypatch main._debug_write to capture the
lines that would have been written, and monkeypatch time.monotonic so the
five-minute summary window can be advanced without a real wait.

The summary count ({n} in "repeated {n} times in the last 5 minutes")
includes the first error that opened the window, not just the suppressed
repeats that followed it, so {n} is always the total failed polls counted in
that window.
"""

import asyncio

import main


def _one_device():
    return [{"id": "dev_a", "host": "10.0.0.4", "metrics_port": 9100}]


def _select(monkeypatch, device_ids):
    monkeypatch.setattr(main.ws_mgr, "_connections", [object()])
    monkeypatch.setattr(main.ws_mgr, "selected_device_ids", lambda: set(device_ids))


def _debug_recorder(monkeypatch):
    lines = []
    monkeypatch.setattr(main, "_debug_write", lambda msg: lines.append(msg))
    return lines


def _clock(monkeypatch, start=1000.0):
    state = {"now": start}

    def fake_monotonic():
        return state["now"]
    monkeypatch.setattr(main.time, "monotonic", fake_monotonic)
    return state


async def _poll(monkeypatch, error_or_none):
    async def fake_fetch_metrics(host, port, prev=None):
        if error_or_none is None:
            return {"cpu_percent": 5.0}
        return {"error": error_or_none}
    monkeypatch.setattr(main, "fetch_metrics", fake_fetch_metrics)

    async def broadcast(msg):
        pass
    monkeypatch.setattr(main.ws_mgr, "broadcast", broadcast)

    await main._poll_once()


def _setup(monkeypatch):
    monkeypatch.setattr(main, "_devices_cache", _one_device())
    monkeypatch.setattr(main, "_metrics_cache", {})
    monkeypatch.setattr(main, "_metrics_error_state", {})
    _select(monkeypatch, {"dev_a"})
    lines = _debug_recorder(monkeypatch)
    clock = _clock(monkeypatch)
    return lines, clock


def test_first_error_is_logged_immediately(monkeypatch):
    lines, clock = _setup(monkeypatch)

    asyncio.run(_poll(monkeypatch, "connection refused"))

    assert lines == ["METRICS [dev_a] 10.0.0.4:9100 → connection refused"]


def test_identical_error_is_suppressed_before_window_elapses(monkeypatch):
    lines, clock = _setup(monkeypatch)

    asyncio.run(_poll(monkeypatch, "connection refused"))
    clock["now"] += 10
    asyncio.run(_poll(monkeypatch, "connection refused"))

    assert len(lines) == 1  # no new line for the identical repeat


def test_summary_logged_after_five_minutes_and_window_resets(monkeypatch):
    lines, clock = _setup(monkeypatch)

    asyncio.run(_poll(monkeypatch, "connection refused"))  # 1st failed poll
    clock["now"] += 60
    asyncio.run(_poll(monkeypatch, "connection refused"))  # 2nd
    clock["now"] += 60
    asyncio.run(_poll(monkeypatch, "connection refused"))  # 3rd
    clock["now"] += main._METRICS_ERROR_SUMMARY_SECONDS  # push past the window

    asyncio.run(_poll(monkeypatch, "connection refused"))  # 4th, triggers summary

    assert len(lines) == 2
    assert lines[1] == (
        "METRICS [dev_a] 10.0.0.4:9100 → still failing: connection refused "
        "(repeated 4 times in the last 5 minutes)"
    )

    # Window has reset: the next identical failure does not immediately
    # produce another summary line.
    clock["now"] += 1
    asyncio.run(_poll(monkeypatch, "connection refused"))
    assert len(lines) == 2


def test_recovery_logged_once_then_nothing_further(monkeypatch):
    lines, clock = _setup(monkeypatch)

    asyncio.run(_poll(monkeypatch, "connection refused"))
    clock["now"] += 10
    asyncio.run(_poll(monkeypatch, "connection refused"))
    clock["now"] += 10
    asyncio.run(_poll(monkeypatch, None))  # recovers

    assert lines[-1] == "METRICS [dev_a] 10.0.0.4:9100 → recovered after 2 failed polls"

    # A later success with no prior error state logs nothing further.
    asyncio.run(_poll(monkeypatch, None))
    assert len(lines) == 2


def test_changed_error_string_is_logged_immediately_as_new(monkeypatch):
    lines, clock = _setup(monkeypatch)

    asyncio.run(_poll(monkeypatch, "dns lookup failed"))
    clock["now"] += 10
    asyncio.run(_poll(monkeypatch, "connection blocked"))

    assert lines == [
        "METRICS [dev_a] 10.0.0.4:9100 → dns lookup failed",
        "METRICS [dev_a] 10.0.0.4:9100 → connection blocked",
    ]


def test_deselected_device_logs_fresh_first_error_on_return(monkeypatch):
    lines, clock = _setup(monkeypatch)

    asyncio.run(_poll(monkeypatch, "connection refused"))
    assert len(lines) == 1

    # Device is deselected: the next poll cycle prunes its error state.
    _select(monkeypatch, set())
    asyncio.run(_poll(monkeypatch, None))  # no devices selected, nothing fetched
    assert len(lines) == 1
    assert "dev_a" not in main._metrics_error_state

    # Device is selected again and fails again: logs as a first error, not
    # a summary, since its tracking was reset.
    _select(monkeypatch, {"dev_a"})
    asyncio.run(_poll(monkeypatch, "connection refused"))

    assert lines[-1] == "METRICS [dev_a] 10.0.0.4:9100 → connection refused"
    assert lines.count("METRICS [dev_a] 10.0.0.4:9100 → connection refused") == 2
