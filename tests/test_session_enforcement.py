"""Tests for the Task 8 Phase 2 current-session enforcement in
ssh_manager.SSHManager: the per-line identity gate in `_exec`, and the
generation stamp-and-drop-at-delivery mechanism in `_deliver`.

`_Session.connect` and paramiko are never exercised for real. Nothing here
touches the network.

`_FakeChannel` stands in for the raw paramiko channel that `_exec` now reads
directly (`recv_ready`/`recv`/`recv_stderr_ready`/`recv_stderr`/
`exit_status_ready`/`recv_exit_status`), fed from a scripted list of events:
("out", bytes), ("err", bytes), or ("exit", code) — an exit marker that can
sit ahead of later out/err events, so a test can model the remote process
exiting while buffered output is still waiting to be drained. It is reused by
tests/test_output_draining.py."""

import asyncio

import ssh_manager


class _FakeChannel:
    """Scripted stand-in for a paramiko channel. `events` is consumed in
    order; an ("exit", code) entry marks the exit status ready without
    requiring a recv call, and may be followed by more out/err events still
    waiting to be drained. `on_event(stream)`, if given, is called just
    before each `recv`/`recv_stderr` returns its chunk, so a test can hook
    behavior (like swapping the manager's current session) in between
    chunks."""

    def __init__(self, events, exit_code=0, on_event=None):
        self._events    = list(events)
        self._exit_code = exit_code
        self._exited    = False
        self._closed    = False
        self._on_event  = on_event

    def _advance_exit_markers(self):
        while self._events and self._events[0][0] == "exit":
            _, code = self._events.pop(0)
            self._exited = True
            if code is not None:
                self._exit_code = code

    def recv_ready(self):
        if self._closed:
            return False
        self._advance_exit_markers()
        return bool(self._events) and self._events[0][0] == "out"

    def recv_stderr_ready(self):
        if self._closed:
            return False
        self._advance_exit_markers()
        return bool(self._events) and self._events[0][0] == "err"

    def recv(self, _n):
        if self._on_event:
            self._on_event("out")
        if not self._events:
            return b""
        _, data = self._events.pop(0)
        return data

    def recv_stderr(self, _n):
        if self._on_event:
            self._on_event("err")
        if not self._events:
            return b""
        _, data = self._events.pop(0)
        return data

    def exit_status_ready(self):
        if self._closed:
            return True
        self._advance_exit_markers()
        if not self._events:
            # No more scripted events at all: the remote process is treated
            # as having exited, mirroring a real channel with nothing left
            # buffered and no more data coming.
            self._exited = True
        return self._exited

    def recv_exit_status(self):
        return self._exit_code

    def close(self):
        self._closed = True
        self._events = []


class _FakeStdoutStream:
    """Stands in for paramiko's stdout ChannelFile: `_exec` only reads
    `.channel` off of it."""

    def __init__(self, channel):
        self.channel = channel


class _FakeStdin:
    def write(self, _s):
        pass

    def flush(self):
        pass


class _FakeClient:
    """Stands in for paramiko.SSHClient. `exec_command` hands back the same
    fake channel wrapped as stdout, plus an unused stderr placeholder (the
    stderr ChannelFile object itself is never touched by `_exec` anymore)."""

    def __init__(self, channel):
        self._channel = channel

    def exec_command(self, cmd, get_pty=False):
        return _FakeStdin(), _FakeStdoutStream(self._channel), object()

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

    calls = {"n": 0}

    def on_event(_stream):
        calls["n"] += 1
        # Before the *second* chunk is handed back, a reconnect replaces this
        # device's session. The first line was already delivered while this
        # worker's session was still current.
        if calls["n"] == 2 and mgr.sessions.get("d1") is sess:
            newer = ssh_manager._Session(device, "pw", "owner-1", "d1")
            newer.state = "connected"
            newer.generation = 2
            mgr.sessions["d1"] = newer

    channel = _FakeChannel(
        [("out", b"first line\n"), ("out", b"second line\n"), ("out", b"third line\n")],
        on_event=on_event,
    )
    sess.client = _FakeClient(channel)

    emitted = []
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
