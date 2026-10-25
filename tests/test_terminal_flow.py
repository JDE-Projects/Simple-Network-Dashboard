"""Browser acknowledgement pacing for terminal output."""

import threading
import time

from simple_network_dashboard import config, ssh_manager
from simple_network_dashboard.terminal_history import TerminalHistory
from tests.test_session_enforcement import _FakeChannel, _FakeClient


def _manager(watching=lambda _owner: True, debug=None):
    async def noop(_message):
        pass
    return ssh_manager.SSHManager(noop, debug or (lambda _text: None), watching)


def _session(manager):
    sess = ssh_manager._Session({"id": "d1", "host": "host", "username": "user"}, "pw", "owner", "d1")
    sess.state, sess.generation, sess.run_id, sess.busy = "connected", 7, "run-1", True
    manager.sessions["d1"] = sess
    return sess


def _sent(sess, *items):
    with sess.term_condition:
        for seq, size in items:
            sess.sent_seq = seq
            sess.sent_bytes[seq] = size


def test_reader_pauses_until_ack_then_delivers_all_output_in_order(monkeypatch):
    monkeypatch.setattr(config, "TERM_BATCH_MAX_CHARS", 1)
    monkeypatch.setattr(config, "TERM_UNACKED_MAX_BYTES", 0)
    manager = _manager()
    sess = _session(manager)
    channel = _FakeChannel([("out", b"a"), ("out", b"b"), ("out", b"c")])
    sess.client = _FakeClient(channel)
    delivered, first = [], threading.Event()
    manager._push = lambda _message: None
    manager._status = lambda *_args, **_kwargs: None
    manager._log = lambda *_args, **_kwargs: None

    def push(message, should_continue):
        delivered.append(message)
        first.set()
        return should_continue()

    manager._push_terminal = push
    worker = threading.Thread(target=manager._exec, args=(sess, "cmd", "Label", False), kwargs={"run_id": "run-1"})
    worker.start()
    assert first.wait(1)
    time.sleep(0.05)
    assert [item["data"] for item in delivered] == ["a"]
    manager.terminal_ack("d1", "run-1", 1, "owner")
    deadline = time.monotonic() + 1
    while len(delivered) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [item["data"] for item in delivered] == ["a", "b"]
    manager.terminal_ack("d1", "run-1", 2, "owner")
    worker.join(1)
    assert not worker.is_alive()
    assert [item["data"] for item in delivered] == ["a", "b", "c"]


def test_ack_validation_ignores_invalid_owner_run_sequence_and_lower_ack():
    manager = _manager()
    sess = _session(manager)
    _sent(sess, (1, 10), (2, 10))
    for run_id, seq, owner in (("run-1", 1, "other"), ("old", 1, "owner"), ("run-1", 3, "owner"), ("run-1", "1", "owner")):
        manager.terminal_ack("d1", run_id, seq, owner)
    assert sess.acked_seq == 0
    manager.terminal_ack("d1", "run-1", 2, "owner")
    manager.terminal_ack("d1", "run-1", 1, "owner")
    assert sess.acked_seq == 2 and sess.sent_bytes == {}
    manager.sessions["d1"] = ssh_manager._Session(sess.device, "pw", "owner", "d1")
    manager.terminal_ack("d1", "run-1", 2, "owner")
    assert sess.acked_seq == 2


def test_no_owner_socket_does_not_wait(monkeypatch):
    monkeypatch.setattr(config, "TERM_UNACKED_MAX_BYTES", 0)
    manager = _manager(lambda _owner: False)
    sess = _session(manager)
    _sent(sess, (1, 1))
    assert manager._wait_for_terminal_ack("d1", sess, "run-1") is True


def test_stall_logs_once_and_a_later_ack_restores_pacing(monkeypatch):
    monkeypatch.setattr(config, "TERM_UNACKED_MAX_BYTES", 0)
    monkeypatch.setattr(config, "TERM_ACK_STALL_SECONDS", 0.01)
    debug = []
    manager = _manager(debug=debug.append)
    sess = _session(manager)
    _sent(sess, (1, 1))
    assert manager._wait_for_terminal_ack("d1", sess, "run-1") is True
    assert debug == ["terminal acknowledgements stalled"]
    assert manager._wait_for_terminal_ack("d1", sess, "run-1") is True
    assert debug == ["terminal acknowledgements stalled"]
    manager.terminal_ack("d1", "run-1", 1, "owner")
    assert not sess.ack_stalled


def test_snapshot_acknowledges_its_output_and_stop_wakes_a_waiting_reader(monkeypatch):
    monkeypatch.setattr(config, "TERM_UNACKED_MAX_BYTES", 0)
    manager = _manager()
    sess = _session(manager)
    sess.history = TerminalHistory()
    sess.history.feed("history", 1)
    _sent(sess, (1, 7))
    assert manager.owner_terminal_snapshot("owner")[0]["seq"] == 1
    assert sess.acked_seq == 1
    _sent(sess, (2, 1))
    waiting = threading.Thread(target=manager._wait_for_terminal_ack, args=("d1", sess, "run-1"))
    waiting.start()
    time.sleep(0.05)
    assert waiting.is_alive()
    sess.channel = _FakeChannel([])
    sess.auth_complete = True
    sess.writer = type("Writer", (), {"interrupt": lambda self: None})()
    timers = []

    class Timer:
        def __init__(self, _delay, callback, args):
            self.callback, self.args = callback, args
            self.daemon = False
            timers.append(self)
        def start(self):
            pass

    monkeypatch.setattr(ssh_manager.threading, "Timer", Timer)
    assert manager.cancel("d1", "run-1", "owner") == {"ok": True}
    waiting.join(1)
    assert not waiting.is_alive()
    timers[0].callback(*timers[0].args)
    assert sess.forced and sess.channel._closed
