"""Tests for the Task 8 Phase 2 current-session enforcement in
ssh_manager.SSHManager: the per-line identity gate in `_exec`, and the
generation stamp-and-drop-at-delivery mechanism in `_deliver`.

`_Session.connect` and paramiko are never exercised for real. Nothing here
touches the network."""

import asyncio

import ssh_manager


class _FakeChannel:
    def close(self):
        pass


class _FakeStdout:
    """Stands in for paramiko's stdout stream: yields queued lines one at a
    time from `readline`, then an empty string (EOF)."""

    def __init__(self, lines):
        self._lines = list(lines)
        self.channel = _FakeChannel()

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return ""


class _FakeStderr:
    def read(self):
        return b""


class _FakeStdin:
    def write(self, _s):
        pass

    def flush(self):
        pass


class _FakeClient:
    """Stands in for paramiko.SSHClient. `exec_command` returns a stdout
    stream that calls back into the test between lines, so a test can swap
    the manager's current session mid-command."""

    def __init__(self, lines, on_readline=None):
        self._stdout = _FakeStdout(lines)
        self._on_readline = on_readline

    def exec_command(self, cmd, get_pty=False):
        return _FakeStdin(), self._stdout, _FakeStderr()

    def close(self):
        pass


def _mgr():
    async def _noop(_msg):
        pass
    return ssh_manager.SSHManager(_noop, lambda _t: None)


def _device(device_id="d1"):
    return {"id": device_id, "host": "10.0.0.9", "username": "pi"}


def test_worker_stops_emitting_once_its_session_is_replaced(monkeypatch):
    mgr = _mgr()
    device = _device()

    sess = ssh_manager._Session(device, "pw", "owner-1", "d1")
    sess.state = "connected"
    sess.generation = 1
    mgr.sessions["d1"] = sess

    lines = ["first line\n", "second line\n", "third line\n"]
    sess.client = _FakeClient(lines)

    emitted = []
    real_readline = sess.client._stdout.readline
    calls = {"n": 0}

    def readline_and_swap():
        calls["n"] += 1
        # Before the *second* readline call returns, a reconnect replaces
        # this device's session. The first line was already delivered while
        # this worker's session was still current.
        if calls["n"] == 2 and mgr.sessions.get("d1") is sess:
            newer = ssh_manager._Session(device, "pw", "owner-1", "d1")
            newer.state = "connected"
            newer.generation = 2
            mgr.sessions["d1"] = newer
        return real_readline()

    sess.client._stdout.readline = readline_and_swap
    monkeypatch.setattr(mgr, "_log", lambda did, text, level="out", **k: emitted.append(text))
    monkeypatch.setattr(mgr, "_status", lambda *a, **k: None)

    mgr._exec(sess, "echo hi", "Custom command", False)

    # Only the initial "$ label" line and the first output line were emitted
    # while this session was still current; nothing after the swap was.
    assert "first line" in emitted
    assert "second line" not in emitted
    assert "third line" not in emitted
    assert not any("finished" in t or "exited" in t for t in emitted)


def test_delivery_drops_a_message_stamped_with_a_stale_generation():
    mgr = _mgr()
    device = _device()

    current = ssh_manager._Session(device, "pw", "owner-1", "d1")
    current.state = "connected"
    current.generation = 2
    mgr.sessions["d1"] = current

    broadcast_calls = []

    async def fake_broadcast(msg):
        broadcast_calls.append(msg)

    mgr._broadcast = fake_broadcast

    stale_msg = {"type": "ssh_log", "device_id": "d1", "text": "late output",
                 "level": "out", "_gen": 1}
    asyncio.run(mgr._deliver(stale_msg))

    assert broadcast_calls == []


def test_delivery_forwards_a_message_with_a_matching_generation_and_strips_gen():
    mgr = _mgr()
    device = _device()

    current = ssh_manager._Session(device, "pw", "owner-1", "d1")
    current.state = "connected"
    current.generation = 2
    mgr.sessions["d1"] = current

    broadcast_calls = []

    async def fake_broadcast(msg):
        broadcast_calls.append(msg)

    mgr._broadcast = fake_broadcast

    fresh_msg = {"type": "ssh_log", "device_id": "d1", "text": "current output",
                 "level": "out", "_gen": 2}
    asyncio.run(mgr._deliver(fresh_msg))

    assert len(broadcast_calls) == 1
    delivered = broadcast_calls[0]
    assert delivered["text"] == "current output"
    assert "_gen" not in delivered


def test_trailing_connected_status_is_dropped_if_replaced_before_delivery(monkeypatch):
    mgr = _mgr()
    device = _device()

    sess = ssh_manager._Session(device, "pw", "owner-1", "d1")
    sess.state = "connected"
    sess.generation = 1
    sess.busy = True
    mgr.sessions["d1"] = sess

    # _finish_exec's identity check passes (sess is still current at that
    # instant)...
    mgr._finish_exec(sess)

    # ...but before the scheduled broadcast is delivered, the session is
    # replaced by a reconnect.
    newer = ssh_manager._Session(device, "pw", "owner-1", "d1")
    newer.state = "connected"
    newer.generation = 2
    mgr.sessions["d1"] = newer

    broadcast_calls = []

    async def fake_broadcast(msg):
        broadcast_calls.append(msg)

    mgr._broadcast = fake_broadcast

    stamped_msg = {"type": "ssh_status", "device_id": "d1", "state": "connected",
                    "_gen": sess.generation}
    asyncio.run(mgr._deliver(stamped_msg))

    assert broadcast_calls == []


def test_unstamped_message_always_passes_through_delivery():
    mgr = _mgr()

    broadcast_calls = []

    async def fake_broadcast(msg):
        broadcast_calls.append(msg)

    mgr._broadcast = fake_broadcast

    # No current session at all for this device, and no "_gen" key: an
    # unstamped message is never dropped for a generation mismatch.
    plain_msg = {"type": "ssh_lock", "device_id": "d1", "locked": False}
    asyncio.run(mgr._deliver(plain_msg))

    assert broadcast_calls == [{"type": "ssh_lock", "device_id": "d1", "locked": False}]
