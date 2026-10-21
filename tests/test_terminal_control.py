"""Terminal ownership, writer, Stop, and silence accounting coverage."""

import socket
import threading

import config
import main
import pytest
import ssh_manager
from terminal_history import TerminalHistory
from terminal_run import TerminalWriter
from tests.test_session_enforcement import _FakeChannel, _FakeClient
from tests.test_websocket_protection import _authenticated_client


class _Channel:
    def __init__(self, sends=None):
        self.sends = list(sends or [])
        self.writes = []
        self.closed = False
        self.resizes = []
        self.wrote = threading.Event()

    def settimeout(self, _value):
        pass

    def send(self, data):
        if self.sends:
            result = self.sends.pop(0)
            if isinstance(result, Exception):
                raise result
            if result:
                self.writes.append(bytes(data[:result]))
                self.wrote.set()
            return result
        self.writes.append(bytes(data))
        self.wrote.set()
        return len(data)

    def resize_pty(self, **kwargs):
        self.resizes.append(kwargs)
        self.wrote.set()

    def close(self):
        self.closed = True


def _manager():
    async def noop(_message):
        pass
    return ssh_manager.SSHManager(noop, lambda _text: None)


def _session(manager, *, auth=True):
    session = ssh_manager._Session({"id": "d1", "host": "host", "username": "user"}, "pw", "owner", "d1")
    session.state = "connected"
    session.busy = True
    session.run_id = "run-1"
    session.auth_complete = auth
    session.channel = _Channel()
    session.writer = TerminalWriter(session.channel, lambda: None)
    session.writer.start()
    manager.sessions["d1"] = session
    return session


def _stop(session):
    session.writer.close()
    session.writer.thread.join(1)


def test_control_rejections_handover_and_queue_limit(monkeypatch):
    manager = _manager()
    session = _session(manager, auth=False)
    debug = []
    manager._debug_write = debug.append
    assert manager.terminal_input("d1", "run-1", 0, "x", "owner", "socket-a") == {"ok": False}
    session.auth_complete = True
    assert manager.claim_terminal("d1", "run-1", "owner", "socket-a") == {"ok": True, "rev": 1}
    assert manager.terminal_input("d1", "wrong", 1, "x", "owner", "socket-a") == {"ok": False}
    assert manager.terminal_input("d1", "run-1", 1, "x", "other", "socket-a") == {"ok": False}
    assert manager.terminal_input("d1", "run-1", 1, "x", "owner", "socket-b") == {"ok": False}
    assert manager.terminal_input("d1", "run-1", 2, "x", "owner", "socket-a") == {"ok": False}
    assert manager.terminal_input("d1", "run-1", 1, "x" * (config.TERM_INPUT_MAX_CHARS + 1), "owner", "socket-a") == {"ok": False}
    assert manager.terminal_resize("d1", "run-1", 1, "80", 24, "owner", "socket-a") == {"ok": False}
    monkeypatch.setattr(config, "TERM_PENDING_INPUT_MAX_BYTES", 1)
    assert manager.terminal_input("d1", "run-1", 1, "xx", "owner", "socket-a") == {"ok": False, "full": True}
    assert manager.claim_terminal("d1", "run-1", "owner", "socket-b") == {"ok": True, "rev": 2}
    assert all(line == "SSH [d1] terminal input refused" for line in debug)
    _stop(session)


def test_writer_partial_write_interrupt_order_and_deadline(monkeypatch):
    channel = _Channel([1, 1, 10])
    writer = TerminalWriter(channel, lambda: None)
    writer.input("paste")
    writer.interrupt()
    writer.start()
    assert channel.wrote.wait(1)
    writer.close()
    writer.thread.join(1)
    assert b"".join(channel.writes).startswith(b"\x03")

    stuck = []
    channel = _Channel()
    def timeout_send(_data):
        raise socket.timeout()
    channel.send = timeout_send
    writer = TerminalWriter(channel, lambda: stuck.append(True))
    monkeypatch.setattr(config, "TERM_WRITE_DEADLINE", 0.01)
    writer.start()
    assert writer.input("x")
    writer.thread.join(1)
    assert stuck == [True] and not writer.thread.is_alive()


def test_cancel_requires_current_run_and_second_stop_forces(monkeypatch):
    manager = _manager()
    session = _session(manager)
    timers = []

    class Timer:
        def __init__(self, _delay, callback, args):
            self.callback, self.args, self.daemon = callback, args, False
            timers.append(self)
        def start(self):
            pass
        def cancel(self):
            pass

    monkeypatch.setattr(ssh_manager.threading, "Timer", Timer)
    assert manager.cancel("d1", "stale", "owner") == {"ok": False}
    assert manager.cancel("d1", "run-1", "owner") == {"ok": True}
    assert not session.channel.closed
    assert manager.cancel("d1", "run-1", "owner") == {"ok": True}
    assert session.forced and session.channel.closed
    session.run_id = "run-2"
    session.channel.closed = False
    timers[0].callback(*timers[0].args)
    assert not session.channel.closed
    _stop(session)


def test_busy_silence_warns_immediately_and_stay_connected_resets(monkeypatch):
    manager = _manager()
    session = _session(manager)
    manager._owner_touch("owner")
    now = [10000.0]
    monkeypatch.setattr(ssh_manager.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(manager, "_sweep_expired_host_key_prompts", lambda _now: None)
    pushed = []
    manager._push = pushed.append
    manager._owner_activity["owner"]["last_active"] = 0
    session.last_io = 0
    manager.tick_idle()
    assert pushed[0]["type"] == "ssh_idle_warning"
    manager.stay_connected("owner")
    assert session.last_io == now[0]
    _stop(session)


def test_writer_stops_when_channel_reports_closed(monkeypatch):
    stuck = []
    channel = _Channel([0])
    writer = TerminalWriter(channel, lambda: stuck.append(True))
    monkeypatch.setattr(config, "TERM_WRITE_DEADLINE", 5)
    writer.start()
    assert writer.input("x")
    writer.thread.join(1)
    assert not writer.thread.is_alive() and stuck == []


def test_first_stop_of_next_run_is_not_forced(monkeypatch):
    manager = _manager()
    session = _session(manager)
    manager._push = lambda _message: None

    class Timer:
        def __init__(self, _delay, callback, args):
            self.daemon = False
        def start(self):
            pass
        def cancel(self):
            pass

    monkeypatch.setattr(ssh_manager.threading, "Timer", Timer)
    assert manager.cancel("d1", "run-1", "owner") == {"ok": True}
    manager._finish_exec(session)
    assert session.stop_timer is None
    session.busy, session.run_id, session.forced = True, "run-2", False
    session.channel = _Channel()
    session.writer = TerminalWriter(session.channel, lambda: None)
    session.writer.start()
    assert manager.cancel("d1", "run-2", "owner") == {"ok": True}
    assert not session.forced and not session.channel.closed
    _stop(session)


def test_busy_silence_warns_once_then_times_out(monkeypatch):
    manager = _manager()
    session = _session(manager)
    manager._owner_touch("owner")
    now = [10000.0]
    monkeypatch.setattr(ssh_manager.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(manager, "_sweep_expired_host_key_prompts", lambda _now: None)
    monkeypatch.setattr(manager, "_close", lambda *_args: None)
    pushed = []
    manager._push = pushed.append
    session.last_io = now[0] - config.TERM_SILENCE_SECONDS
    for _ in range(20):
        manager.tick_idle()
        now[0] += 2
    types = [message["type"] for message in pushed]
    assert types.count("ssh_idle_warning") == 1
    assert "ssh_idle_timeout" in types
    _stop(session)


def _exec_session(manager, channel):
    session = ssh_manager._Session({"id": "d1", "host": "host", "username": "user"}, "pw", "owner", "d1")
    session.state, session.generation, session.client, session.run_id, session.busy = "connected", 1, _FakeClient(channel), "run-1", True
    manager.sessions["d1"] = session
    return session


def _exec(manager, session, *, sudo=False, phrase="SNDsudopromptA", mark="SNDtermmarkB", on_push=None):
    logs = []
    manager._log = lambda _did, text, level="out", **_kwargs: logs.append((level, text))
    manager._status = lambda *_args, **_kwargs: None
    manager._push = lambda message: on_push(message) if on_push else None
    manager._push_terminal = lambda _message, should_continue: should_continue()
    manager._exec(session, "command", "Label", sudo, sudo_phrase=phrase, sudo_mark=mark, run_id="run-1")
    return logs


def test_sudo_password_is_written_by_writer_and_nopasswd_is_not():
    phrase, mark = "SNDsudopromptA", "SNDtermmarkB"
    manager = _manager()
    channel = _FakeChannel([("out", phrase.encode()), ("out", mark.encode())])
    session = _exec_session(manager, channel)
    _exec(manager, session, sudo=True, phrase=phrase, mark=mark)
    assert channel.stdin_writes == ["pw\n"]

    manager = _manager()
    channel = _FakeChannel([("out", mark.encode())])
    session = _exec_session(manager, channel)
    _exec(manager, session, sudo=True, phrase=phrase, mark=mark)
    assert channel.stdin_writes == []


def test_exec_stop_end_lines_and_debug_messages(monkeypatch):
    manager = _manager()
    channel = _FakeChannel([("out", b"ready")])
    session = _exec_session(manager, channel)
    debug = []
    manager._debug_write = debug.append

    class Timer:
        def __init__(self, _delay, callback, args):
            self.callback, self.args = callback, args
            self.daemon = False
        def start(self):
            pass
        def cancel(self):
            pass

    monkeypatch.setattr(ssh_manager.threading, "Timer", Timer)
    channel._on_event = lambda _stream: manager.cancel("d1", "run-1", "owner")
    logs = _exec(manager, session)
    assert ("warn", "■ Label cancelled.") in logs
    assert not any("exited with code" in text for _level, text in logs)
    assert debug[-1] == "SSH [d1] command cancelled"



def test_exec_forced_stop_end_line_and_debug_message(monkeypatch):
    manager = _manager()
    channel = _FakeChannel([("out", b"ready")])
    session = _exec_session(manager, channel)
    debug = []
    manager._debug_write = debug.append
    timers = []

    class Timer:
        def __init__(self, _delay, callback, args):
            self.callback, self.args = callback, args
            self.daemon = False
            timers.append(self)
        def start(self):
            pass
        def cancel(self):
            pass

    monkeypatch.setattr(ssh_manager.threading, "Timer", Timer)

    def cancel_then_force(message):
        if message["type"] != "term_start":
            return
        manager.cancel("d1", "run-1", "owner")
        timer = timers.pop()
        timer.callback(*timer.args)

    logs = _exec(manager, session, on_push=cancel_then_force)
    assert ("warn", "■ Label did not respond to Ctrl+C, so it was stopped.") in logs
    assert not any("exited with code" in text for _level, text in logs)
    assert debug[-1] == "SSH [d1] command cancelled"


def test_writer_deadline_stops_exec_and_writer_threads_end(monkeypatch):
    manager = _manager()
    channel = _FakeChannel([("out", b"ready")])
    session = _exec_session(manager, channel)
    debug = []
    manager._debug_write = debug.append
    monkeypatch.setattr(config, "TERM_WRITE_DEADLINE", 0.01)

    def timeout_send(_data):
        raise socket.timeout()

    channel.send = timeout_send
    channel.exit_status_ready = lambda: channel._closed
    channel._on_event = lambda _stream: session.writer.input("x")
    writers = []
    original_finish = manager._finish_exec
    manager._finish_exec = lambda saved: (writers.append(saved.writer), original_finish(saved))[1]
    logs = _exec(manager, session)
    assert ("warn", "■ Label did not respond to Ctrl+C, so it was stopped.") in logs
    assert debug[-1] == "SSH [d1] command stopped: device stopped accepting input"
    assert not writers[0].thread.is_alive()


def test_exec_stops_writer_thread_for_normal_and_cancelled_runs(monkeypatch):
    manager = _manager()
    session = _exec_session(manager, _FakeChannel([("out", b"ready")]))
    writers = []
    original_finish = manager._finish_exec

    def finish(saved):
        writers.append(saved.writer)
        original_finish(saved)

    manager._finish_exec = finish
    _exec(manager, session)
    assert not writers[0].thread.is_alive()

    manager = _manager()
    channel = _FakeChannel([("out", b"ready")])
    session = _exec_session(manager, channel)
    writers = []
    original_finish = manager._finish_exec
    manager._finish_exec = lambda saved: (writers.append(saved.writer), original_finish(saved))[1]
    monkeypatch.setattr(ssh_manager.threading, "Timer", type("Timer", (), {
        "__init__": lambda self, *_args: None, "start": lambda self: None, "cancel": lambda self: None,
        "daemon": False,
    }))
    channel._on_event = lambda _stream: manager.cancel("d1", "run-1", "owner")
    _exec(manager, session)
    assert not writers[0].thread.is_alive()


def test_idle_input_refresh_and_replaced_session_refusal(monkeypatch):
    manager = _manager()
    session = _session(manager)
    manager._owner_touch("owner")
    now = [10_000.0]
    monkeypatch.setattr(ssh_manager.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(manager, "_sweep_expired_host_key_prompts", lambda _now: None)
    manager._owner_activity["owner"]["last_active"] = 0
    session.last_io = now[0]
    manager.tick_idle()
    assert manager._owner_activity["owner"]["last_active"] == now[0]
    assert manager.claim_terminal("d1", "run-1", "owner", "socket-a")["ok"]
    now[0] += 1
    assert manager.terminal_input("d1", "run-1", 1, "x", "owner", "socket-a")["ok"]
    assert session.last_io == now[0]
    now[0] += 1
    assert not manager.terminal_input("d1", "run-1", 1, "x", "owner", "socket-b")["ok"]
    assert session.last_io == now[0] - 1
    newer = ssh_manager._Session(session.device, "pw", "owner", "d1")
    newer.state, newer.busy, newer.run_id, newer.generation = "connected", True, "run-2", 2
    manager.sessions["d1"] = newer
    assert not manager.terminal_input("d1", "run-1", 1, "x", "owner", "socket-a")["ok"]
    _stop(session)


def test_accepted_resize_clamps_updates_session_and_writer():
    manager = _manager()
    session = _session(manager)
    assert manager.claim_terminal("d1", "run-1", "owner", "socket-a")["ok"]
    assert manager.terminal_resize("d1", "run-1", 1, 999, 1, "owner", "socket-a")["ok"]
    assert (session.cols, session.rows) == (config.TERM_MAX_COLS, config.TERM_MIN_ROWS)
    assert session.channel.wrote.wait(1)
    _stop(session)
    assert session.channel.resizes == [{"width": config.TERM_MAX_COLS, "height": config.TERM_MIN_ROWS}]


def test_claim_clears_typed_input_but_keeps_resize_and_interrupt():
    channel = _Channel()
    writer = TerminalWriter(channel, lambda: None)
    assert writer.input("discard")
    writer.resize(90, 40)
    writer.interrupt()
    writer.clear_input()
    writer.start()
    assert channel.wrote.wait(1)
    writer.close()
    writer.thread.join(1)
    assert channel.writes == [b"\x03"]
    assert channel.resizes == [{"width": 90, "height": 40}]


def test_websocket_control_fanout_and_input_backpressure(tmp_path, monkeypatch):
    client = _authenticated_client(tmp_path, monkeypatch)
    monkeypatch.setattr(main.ssh_mgr, "owner_ssh_snapshot", lambda _owner: ([], []))
    monkeypatch.setattr(main.ssh_mgr, "owner_terminal_snapshot", lambda _owner: [])
    monkeypatch.setattr(main.ssh_mgr, "claim_terminal", lambda *_args: {"ok": True, "rev": 7})
    monkeypatch.setattr(main.ssh_mgr, "terminal_input", lambda *_args: {"ok": False, "full": True})
    with client.websocket_connect("/ws", headers={"Origin": "https://dashboard.lan"}) as first:
        assert first.receive_json()["type"] == "init"
        with client.websocket_connect("/ws", headers={"Origin": "https://dashboard.lan"}) as second:
            assert second.receive_json()["type"] == "init"
            first.send_json({"type": "term_claim", "device_id": "d1", "run_id": "run-1"})
            first_control = first.receive_json()
            second_control = second.receive_json()
            assert first_control == {"type": "term_control", "device_id": "d1", "run_id": "run-1", "yours": True, "rev": 7}
            assert second_control == {"type": "term_control", "device_id": "d1", "run_id": "run-1", "yours": False, "rev": 7}
            first.send_json({"type": "term_input", "device_id": "d1", "run_id": "run-1", "rev": 7, "data": "x"})
            assert first.receive_json()["type"] == "term_input_full"


def test_exec_history_hides_sudo_authentication_and_precedes_terminal_push():
    phrase, mark = "SNDsudopromptA", "SNDtermmarkB"
    manager = _manager()
    channel = _FakeChannel([("out", f"hidden {phrase}".encode()),
                            ("out", f"pw Sorry, try again. {mark}visible".encode())])
    session = _exec_session(manager, channel)
    session.history = TerminalHistory()
    order = []

    def push(message, should_continue):
        order.append((message["type"], session.history.snapshot()["partial"]))
        return should_continue()

    manager._push_terminal = push
    manager._log = lambda *_args, **_kwargs: None
    manager._status = lambda *_args, **_kwargs: None
    manager._push = lambda _message: None
    manager._exec(session, "command", "Label", True, sudo_phrase=phrase, sudo_mark=mark, run_id="run-1")
    assert order == [("term_data", "visible")]


def test_owner_terminal_snapshot_and_redraw_only_target_current_fullscreen_run():
    manager = _manager()
    session = _session(manager)
    session.cols, session.rows, session.label = 80, 24, "A command"
    session.history = TerminalHistory()
    session.history.feed("saved\npartial", 7)
    session.history.feed("\x1b[?1049h", 8)
    snapshot = manager.owner_terminal_snapshot("owner")
    assert snapshot == [{"device_id": "d1", "run_id": "run-1", "cols": 80, "rows": 24,
                         "lines": ["saved"], "partial": "partial", "fullscreen": True,
                         "seq": 8, "label": "A command"}]
    assert manager.owner_terminal_snapshot("other") == []
    assert manager.request_terminal_redraw("d1", "run-1", "owner")
    assert session.channel.wrote.wait(1)
    _stop(session)
    assert session.channel.resizes == [{"width": 80, "height": 23}, {"width": 80, "height": 24}]
    assert not manager.request_terminal_redraw("d1", "stale", "owner")
    session.history.feed("\x1b[?1049l", 9)
    assert not session.history.snapshot()["fullscreen"]


def test_finish_drops_terminal_history():
    manager = _manager()
    session = _session(manager)
    session.history = TerminalHistory()
    manager._finish_exec(session)
    assert session.history is None


def test_websocket_init_includes_terminal_history_and_redraws_fullscreen(tmp_path, monkeypatch):
    client = _authenticated_client(tmp_path, monkeypatch)
    terminal = {"device_id": "d1", "run_id": "run-1", "cols": 80, "rows": 24,
                "lines": [], "partial": "", "fullscreen": True, "seq": 3, "label": "Label"}
    calls = []
    monkeypatch.setattr(main.ssh_mgr, "owner_ssh_snapshot", lambda _owner: ([], []))
    monkeypatch.setattr(main.ssh_mgr, "owner_terminal_snapshot", lambda _owner: [terminal])
    monkeypatch.setattr(main.ssh_mgr, "request_terminal_redraw", lambda *args: calls.append(args))
    with client.websocket_connect("/ws", headers={"Origin": "https://dashboard.lan"}) as ws:
        assert ws.receive_json()["ssh_terminals"] == [terminal]
    assert len(calls) == 1 and calls[0][:2] == ("d1", "run-1")


def test_websocket_init_does_not_redraw_a_normal_terminal(tmp_path, monkeypatch):
    client = _authenticated_client(tmp_path, monkeypatch)
    terminal = {"device_id": "d1", "run_id": "run-1", "cols": 80, "rows": 24,
                "lines": [], "partial": "", "fullscreen": False, "seq": 3, "label": "Label"}
    monkeypatch.setattr(main.ssh_mgr, "owner_ssh_snapshot", lambda _owner: ([], []))
    monkeypatch.setattr(main.ssh_mgr, "owner_terminal_snapshot", lambda _owner: [terminal])
    monkeypatch.setattr(main.ssh_mgr, "request_terminal_redraw", lambda *_args: pytest.fail("redraw requested"))
    with client.websocket_connect("/ws", headers={"Origin": "https://dashboard.lan"}) as ws:
        assert ws.receive_json()["ssh_terminals"] == [terminal]


def test_debug_lines_do_not_include_terminal_secrets_or_resize_carrier(monkeypatch):
    marker = "PRIVATE-TERMINAL-MARKER"
    manager = _manager()
    session = _session(manager)
    debug = []
    manager._debug_write = debug.append
    assert manager.claim_terminal("d1", "run-1", "owner", "socket-a")["ok"]

    class MarkerInt(int):
        def __repr__(self):
            return marker

    assert manager.terminal_input("d1", "run-1", 1, marker, "owner", "socket-a")["ok"]
    assert manager.terminal_resize("d1", "run-1", 1, MarkerInt(80), MarkerInt(24), "owner", "socket-a")["ok"]
    _stop(session)

    channel = _FakeChannel([("out", b"SNDsudopromptA"), ("out", b"SNDtermmarkB")])
    session = _exec_session(manager, channel)
    session.password = marker
    _exec(manager, session, sudo=True)
    assert all(marker not in line for line in debug)


def test_worst_case_history_init_frame_fits_socket_queue():
    import json
    manager = _manager()
    session = _session(manager)
    session.label, session.cols, session.rows = "Label", 120, 30
    session.history = ssh_manager.TerminalHistory()
    for _ in range(config.TERM_HISTORY_MAX_LINES):
        session.history._lines.append("é" * config.TERM_HISTORY_MAX_LINE_CHARS)
    terminals = manager.owner_terminal_snapshot("owner")
    assert terminals[0]["lines"] and terminals[0]["lines"][-1] == "é" * config.TERM_HISTORY_MAX_LINE_CHARS
    assert len(json.dumps({"type": "init", "ssh_terminals": terminals})) < config.SOCKET_QUEUE_MAX_BYTES // 2
    _stop(session)
