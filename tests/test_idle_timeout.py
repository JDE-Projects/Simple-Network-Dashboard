"""Tests for the idle-timeout sweep in ssh_manager.SSHManager.tick_idle: the
warn-then-timeout owner-activity clock, and the busy-owner-stays-active
exception. Nothing here touches the network or an event loop; `_push` is
monkeypatched to capture messages synchronously."""

import time

import ssh_manager


class _FakeClient:
    """Stands in for paramiko.SSHClient: just enough for _Session.close()."""
    def close(self):
        pass


def _mgr():
    async def _noop(_msg):
        pass
    return ssh_manager.SSHManager(_noop, lambda _t: None)


def _device(device_id="d1"):
    return {"id": device_id, "host": "10.0.0.9", "username": "pi"}


def _connected_session(mgr, device_id="d1", owner="owner-1", generation=1):
    sess = ssh_manager._Session(_device(device_id), "pw", owner, device_id)
    sess.state      = "connected"
    sess.generation = generation
    sess.client     = _FakeClient()
    mgr.sessions[device_id] = sess
    return sess


def test_idle_warning_is_pushed_and_session_stays_up(monkeypatch):
    mgr = _mgr()
    sess = _connected_session(mgr)
    mgr._owner_touch("owner-1")

    pushed = []
    monkeypatch.setattr(mgr, "_push", lambda msg: pushed.append(msg))
    monkeypatch.setattr(mgr, "_status", lambda *a, **k: None)
    monkeypatch.setattr(mgr, "_lock", lambda *a, **k: None)
    monkeypatch.setattr(mgr, "_log", lambda *a, **k: None)

    mgr._owner_activity["owner-1"]["last_active"] = (
        time.monotonic() - (ssh_manager.IDLE_WARN_SECONDS + 1))

    mgr.tick_idle()

    warnings = [m for m in pushed if m.get("type") == "ssh_idle_warning"]
    assert len(warnings) == 1
    assert "seconds" in warnings[0]
    assert warnings[0]["_owner"] == "owner-1"
    assert mgr._owner_activity["owner-1"]["warned"] is True
    assert "d1" in mgr.sessions
    assert mgr.sessions["d1"] is sess


def test_idle_timeout_tears_down_the_session_and_pops_the_owner(monkeypatch):
    mgr = _mgr()
    _connected_session(mgr)
    mgr._owner_touch("owner-1")

    pushed = []
    monkeypatch.setattr(mgr, "_push", lambda msg: pushed.append(msg))
    monkeypatch.setattr(mgr, "_status", lambda *a, **k: None)
    monkeypatch.setattr(mgr, "_lock", lambda *a, **k: None)
    monkeypatch.setattr(mgr, "_log", lambda *a, **k: None)

    mgr._owner_activity["owner-1"]["last_active"] = (
        time.monotonic() - (ssh_manager.IDLE_TIMEOUT_SECONDS + 1))

    mgr.tick_idle()

    assert "d1" not in mgr.sessions
    timeouts = [m for m in pushed if m.get("type") == "ssh_idle_timeout"]
    assert len(timeouts) == 1
    assert timeouts[0]["_owner"] == "owner-1"
    assert "owner-1" not in mgr._owner_activity


def test_busy_owner_is_treated_as_active_and_not_timed_out(monkeypatch):
    mgr = _mgr()
    sess = _connected_session(mgr)
    sess.busy = True
    mgr._owner_touch("owner-1")

    pushed = []
    monkeypatch.setattr(mgr, "_push", lambda msg: pushed.append(msg))
    monkeypatch.setattr(mgr, "_status", lambda *a, **k: None)
    monkeypatch.setattr(mgr, "_lock", lambda *a, **k: None)
    monkeypatch.setattr(mgr, "_log", lambda *a, **k: None)

    mgr._owner_activity["owner-1"]["last_active"] = (
        time.monotonic() - (ssh_manager.IDLE_TIMEOUT_SECONDS + 1))

    mgr.tick_idle()

    timeouts = [m for m in pushed if m.get("type") == "ssh_idle_timeout"]
    assert timeouts == []
    assert "d1" in mgr.sessions
    assert mgr.sessions["d1"] is sess
    assert "owner-1" in mgr._owner_activity
