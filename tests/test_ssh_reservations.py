"""Tests for the Task 8 Phase 1 session-reservation and locking model in
ssh_manager.SSHManager: per-device locks, reserve-before-connect, the
"superseded" result for a same-owner reconnect race, busy-before-thread-start
command reservation, identity-checked closes, and the early-cancel flag.

`_Session.connect` and paramiko are never exercised for real: connect is
monkeypatched to a controllable stub (blocking on a threading.Event where a
test needs to force a specific race ordering), so nothing here touches the
network."""

import threading

import ssh_manager


class _FakeKey:
    """Stands in for a paramiko key object: just enough for _fp()."""
    def get_name(self):
        return "ssh-ed25519"

    def asbytes(self):
        return b"fake-key-bytes"


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


def test_concurrent_connects_from_different_owners_leave_one_live_session(monkeypatch):
    mgr = _mgr()
    device = _device()

    started = threading.Event()
    release = threading.Event()

    def fake_connect(self):
        started.set()
        release.wait(5)
        self.client = _FakeClient()

    monkeypatch.setattr(ssh_manager._Session, "connect", fake_connect)

    results = {}

    def run_owner_a():
        results["a"] = mgr.connect("d1", "pw", device, "owner-a")

    t = threading.Thread(target=run_owner_a)
    t.start()
    assert started.wait(2), "owner-a's connect never started"

    # A second, different owner races in while owner-a's connect is still
    # in flight. The slot is already reserved, so this must be rejected
    # immediately rather than racing the network connect.
    result_b = mgr.connect("d1", "pw", device, "owner-b")
    assert result_b == {"ok": False, "in_use": True,
                         "error": "This device is in use by another session."}

    release.set()
    t.join(5)

    assert results["a"] == {"ok": True}
    assert list(mgr.sessions.keys()) == ["d1"]
    assert mgr.sessions["d1"].owner == "owner-a"
    assert mgr.sessions["d1"].state == "connected"


def test_same_owner_reconnect_supersedes_the_in_flight_connect(monkeypatch):
    mgr = _mgr()
    device = _device()

    starts   = [threading.Event(), threading.Event()]
    releases = [threading.Event(), threading.Event()]
    call_index = {"n": 0}
    index_lock = threading.Lock()

    def fake_connect(self):
        with index_lock:
            idx = call_index["n"]
            call_index["n"] += 1
        starts[idx].set()
        releases[idx].wait(5)
        self.client = _FakeClient()

    monkeypatch.setattr(ssh_manager._Session, "connect", fake_connect)

    results = {}

    def run(key):
        results[key] = mgr.connect("d1", "pw", device, "owner-1")

    t_a = threading.Thread(target=run, args=("a",))
    t_a.start()
    assert starts[0].wait(2), "first connect never started"

    # Same owner reconnects while the first attempt is still mid-flight.
    # This retires the first reservation and installs its own, atomically,
    # under the same device lock.
    t_b = threading.Thread(target=run, args=("b",))
    t_b.start()
    assert starts[1].wait(2), "second connect never started"

    # Let the newer (b) connect finish first, then the older (a) one.
    releases[1].set()
    t_b.join(5)
    releases[0].set()
    t_a.join(5)

    assert results["b"] == {"ok": True}
    assert results["a"]["ok"] is False
    assert results["a"].get("superseded") is True
    assert "in_use" not in results["a"]

    assert list(mgr.sessions.keys()) == ["d1"]
    assert mgr.sessions["d1"].state == "connected"


def test_connect_failure_releases_slot_but_unknown_host_key_keeps_it(monkeypatch):
    mgr = _mgr()
    device = _device()

    def boom(self):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(ssh_manager._Session, "connect", boom)
    result = mgr.connect("d1", "pw", device, "owner-1")

    assert result["ok"] is False
    assert "d1" not in mgr.sessions

    def unknown_key(self):
        raise ssh_manager.UnknownHostKey(device["host"], _FakeKey())

    monkeypatch.setattr(ssh_manager._Session, "connect", unknown_key)
    result2 = mgr.connect("d1", "pw", device, "owner-1")

    assert result2["ok"] is False
    assert result2["host_key_unknown"] is True
    assert "d1" in mgr.sessions
    assert mgr.sessions["d1"].state == "host_key_pending"
    assert mgr._pending["d1"].host == device["host"]


def test_run_command_rejects_a_second_call_while_the_first_is_busy(monkeypatch):
    mgr = _mgr()
    device = _device()
    sess = ssh_manager._Session(device, "pw", "owner-1", "d1")
    sess.state  = "connected"
    sess.client = object()
    mgr.sessions["d1"] = sess

    entered = threading.Event()
    release = threading.Event()

    def fake_exec(self, sess_arg, cmd, label, feed_sudo, cmd_id=None):
        entered.set()
        release.wait(5)

    monkeypatch.setattr(ssh_manager.SSHManager, "_exec", fake_exec)

    result1 = mgr.run_command("d1", "echo hi", False, owner="owner-1")
    assert result1 == {"ok": True}
    assert entered.wait(2), "worker thread never started"

    # busy was reserved under the lock before the worker thread was even
    # started, so this second call must be rejected even though the worker
    # (stubbed here) hasn't done any real work yet.
    result2 = mgr.run_command("d1", "echo hi again", False, owner="owner-1")
    assert result2 == {"ok": False, "error": "A command is already running."}

    release.set()


def test_close_with_a_stale_identity_does_not_tear_down_the_newer_session():
    mgr = _mgr()
    device = _device()

    old_sess = ssh_manager._Session(device, "pw", "owner-1", "d1")
    old_sess.state  = "connected"
    old_sess.client = object()

    new_sess = ssh_manager._Session(device, "pw", "owner-2", "d1")
    new_sess.state  = "connected"
    new_sess.client = object()
    mgr.sessions["d1"] = new_sess

    # Simulate a delayed close (e.g. an idle-timeout snapshot) firing against
    # a session that has since been replaced for the same device id.
    mgr._close("d1", old_sess)

    assert mgr.sessions["d1"] is new_sess
    assert new_sess.state == "connected"
    assert new_sess.client is not None


def test_stale_close_does_not_broadcast_idle_or_unlock(monkeypatch):
    mgr = _mgr()
    device = _device()

    old_sess = ssh_manager._Session(device, "pw", "owner-1", "d1")
    old_sess.state  = "connected"
    old_sess.client = _FakeClient()

    new_sess = ssh_manager._Session(device, "pw", "owner-2", "d1")
    new_sess.state  = "connected"
    new_sess.client = _FakeClient()
    mgr.sessions["d1"] = new_sess

    statuses = []
    locks = []
    monkeypatch.setattr(mgr, "_status", lambda did, state, owner=None: statuses.append((did, state)))
    monkeypatch.setattr(mgr, "_lock", lambda did, locked: locks.append((did, locked)))

    # Stale close against a replaced session: the newer session stays live and
    # no idle/unlock is broadcast, so the live session's UI state is untouched.
    mgr._close("d1", old_sess)
    assert statuses == []
    assert locks == []

    # A genuine close of the current session does announce idle and unlock.
    mgr._close("d1", new_sess)
    assert ("d1", "idle") in statuses
    assert ("d1", False) in locks


def test_cancel_before_channel_is_wired_still_takes_effect(monkeypatch):
    mgr = _mgr()
    device = _device()
    sess = ssh_manager._Session(device, "pw", "owner-1", "d1")
    sess.state  = "connected"
    sess.client = object()
    mgr.sessions["d1"] = sess

    # Reserve the run exactly as run_command() would, without actually
    # starting the worker thread.
    sess.busy             = True
    sess.cancelled        = False
    sess.cancel_requested = False

    result = mgr.cancel("d1", owner="owner-1")
    assert result == {"ok": True}
    assert sess.cancel_requested is True
    assert sess.cancelled is True

    logs = []
    monkeypatch.setattr(mgr, "_log", lambda *a, **k: logs.append(a))
    monkeypatch.setattr(mgr, "_status", lambda *a, **k: None)
    monkeypatch.setattr(mgr, "_finish_exec", lambda s: None)

    # _exec must honor cancel_requested before ever touching sess.channel
    # (sess.client is a plain object() with no exec_command, so reaching
    # that call would raise and fail this test).
    mgr._exec(sess, "echo hi", "Custom command", False)

    assert sess.channel is None
    assert any("cancelled" in str(call) for call in logs)


def test_locked_device_ids_and_owner_connected_ids_exclude_reserved_sessions():
    mgr = _mgr()
    device = _device()

    connecting = ssh_manager._Session(device, "pw", "owner-1", "d1")
    connecting.state = "connecting"
    pending = ssh_manager._Session(device, "pw", "owner-1", "d2")
    pending.state = "host_key_pending"
    connected = ssh_manager._Session(device, "pw", "owner-1", "d3")
    connected.state = "connected"

    mgr.sessions["d1"] = connecting
    mgr.sessions["d2"] = pending
    mgr.sessions["d3"] = connected

    assert mgr.locked_device_ids() == ["d3"]
    assert mgr.owner_connected_ids("owner-1") == ["d3"]
