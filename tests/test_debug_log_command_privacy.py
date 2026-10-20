"""Command diagnostics must not disclose browser-visible SSH content."""

from __future__ import annotations

import asyncio

import debug_log
import main
import pytest
import ssh_manager
from tests.test_session_enforcement import _FakeChannel, _FakeClient


MARKER = "ZQX_MARKER_7731"


@pytest.fixture(autouse=True)
def _clean_debug_logger():
    yield
    for handler in list(debug_log._debug_logger.handlers):
        debug_log._debug_logger.removeHandler(handler)
        handler.close()
    debug_log._debug_handler = None
    debug_log._debug_failed_notice = None
    debug_log._debug_loop = None


def _enable(tmp_path, monkeypatch) -> str:
    monkeypatch.setattr(main, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(debug_log, "_debug_handler", None)
    return asyncio.run(main.toggle_debug(main.DebugIn(enabled=True)))["path"]


def _manager():
    async def _noop(_message):
        pass

    return ssh_manager.SSHManager(_noop, debug_log._debug_write)


def _session(manager, *, password=MARKER):
    device = {"id": "device-7", "host": "host.example", "username": "operator"}
    sess = ssh_manager._Session(device, password, "owner-1", "device-7")
    sess.state = "connected"
    sess.generation = 1
    manager.sessions[sess.device_id] = sess
    return sess


def _command_lines(path):
    return [line for line in open(path, encoding="utf-8") if "SSH [device-7] command" in line]


def _assert_one_run(path, start, end):
    lines = _command_lines(path)
    assert sum(line.rstrip().endswith(start) for line in lines) == 1
    assert sum(line.rstrip().endswith(end) for line in lines) == 1
    assert len(lines) == 2


def test_success_logs_only_raw_length_and_outcome(tmp_path, monkeypatch):
    path = _enable(tmp_path, monkeypatch)
    manager = _manager()
    sess = _session(manager)
    sess.client = _FakeClient(_FakeChannel([
        ("out", f"{MARKER} split ".encode()),
        ("out", b"without newline"),
        ("err", f"stderr {MARKER}\n".encode()),
    ], exit_code=0))
    raw = f"echo {MARKER}"

    manager._exec(sess, raw, f"label {MARKER}", False, raw_command_length=len(raw))

    content = open(path, encoding="utf-8").read()
    assert MARKER not in content
    _assert_one_run(
        path,
        f"SSH [device-7] command started (sudo: no, {len(raw)} chars)",
        "SSH [device-7] command finished (exit code 0)",
    )


def test_sudo_acceptance_and_rejection_have_private_terminal_lines(tmp_path, monkeypatch):
    path = _enable(tmp_path, monkeypatch)
    raw = f"echo {MARKER}"

    accepted = _manager()
    accepted_sess = _session(accepted)
    accepted_sess.client = _FakeClient(_FakeChannel([
        ("out", b"__SND_SUDO_prompt__\n"),
        ("out", f"{MARKER}\n".encode()),
    ]))
    accepted._exec(
        accepted_sess, f"sudo -S -p __SND_SUDO_prompt__ {raw}", f"label {MARKER}", True,
        sudo_prompt="__SND_SUDO_prompt__", raw_command_length=len(raw),
    )

    rejected = _manager()
    rejected_sess = _session(rejected)
    rejected_sess.client = _FakeClient(_FakeChannel([
        ("out", b"__SND_SUDO_prompt__\n"),
        ("out", b"__SND_SUDO_prompt__\n"),
    ]))
    rejected._exec(
        rejected_sess, f"sudo -S -p __SND_SUDO_prompt__ {raw}", f"label {MARKER}", True,
        sudo_prompt="__SND_SUDO_prompt__", raw_command_length=len(raw),
    )

    content = open(path, encoding="utf-8").read()
    assert MARKER not in content
    lines = _command_lines(path)
    assert sum(line.rstrip().endswith(f"SSH [device-7] command started (sudo: yes, {len(raw)} chars)") for line in lines) == 2
    assert sum(line.rstrip().endswith("SSH [device-7] command finished (exit code 0)") for line in lines) == 1
    assert sum(line.rstrip().endswith("SSH [device-7] command stopped: sudo did not accept the password") for line in lines) == 1
    assert len(lines) == 4


def test_cancel_before_channel_and_mid_run_each_log_one_cancelled_end(tmp_path, monkeypatch):
    path = _enable(tmp_path, monkeypatch)
    raw = f"echo {MARKER}"

    before = _manager()
    before_sess = _session(before)
    before_sess.client = object()
    before_sess.cancel_requested = True
    before._exec(before_sess, raw, f"label {MARKER}", False, raw_command_length=len(raw))

    mid = _manager()
    mid_sess = _session(mid)

    def _cancel(_stream):
        mid_sess.cancelled = True

    mid_sess.client = _FakeClient(_FakeChannel([("out", f"{MARKER}\n".encode())], on_event=_cancel))
    mid._exec(mid_sess, raw, f"label {MARKER}", False, raw_command_length=len(raw))

    content = open(path, encoding="utf-8").read()
    assert MARKER not in content
    lines = _command_lines(path)
    assert sum(line.rstrip().endswith(f"SSH [device-7] command started (sudo: no, {len(raw)} chars)") for line in lines) == 2
    assert sum(line.rstrip().endswith("SSH [device-7] command cancelled") for line in lines) == 2
    assert len(lines) == 4


class _ExecFailure(Exception):
    pass


class _ReadFailure(Exception):
    pass


class _ExitFailure(Exception):
    pass


class _ExplodingClient:
    def exec_command(self, *_args, **_kwargs):
        raise _ExecFailure(MARKER)


class _ReadFailureChannel(_FakeChannel):
    def recv(self, _size):
        raise _ReadFailure(MARKER)


class _ExitFailureChannel(_FakeChannel):
    def recv_exit_status(self):
        raise _ExitFailure(MARKER)


@pytest.mark.parametrize(
    ("client", "failure"),
    [
        (_ExplodingClient(), _ExecFailure),
        (_FakeClient(_ReadFailureChannel([("out", b"data\n")])), _ReadFailure),
        (_FakeClient(_ExitFailureChannel([])), _ExitFailure),
    ],
)
def test_command_exceptions_log_only_the_exception_type(tmp_path, monkeypatch, client, failure):
    path = _enable(tmp_path, monkeypatch)
    manager = _manager()
    sess = _session(manager)
    sess.client = client
    raw = f"echo {MARKER}"

    manager._exec(sess, raw, f"label {MARKER}", False, raw_command_length=len(raw))

    content = open(path, encoding="utf-8").read()
    assert MARKER not in content
    _assert_one_run(
        path,
        f"SSH [device-7] command started (sudo: no, {len(raw)} chars)",
        f"SSH [device-7] command failed ({failure.__name__})",
    )


def test_not_connected_does_not_start_and_has_one_terminal_line(tmp_path, monkeypatch):
    path = _enable(tmp_path, monkeypatch)
    manager = _manager()
    sess = _session(manager)
    sess.client = None

    manager._exec(sess, f"echo {MARKER}", f"label {MARKER}", False)

    content = open(path, encoding="utf-8").read()
    assert MARKER not in content
    assert len(_command_lines(path)) == 1
    assert _command_lines(path)[0].rstrip().endswith("SSH [device-7] command not started: not connected")


def test_replaced_session_still_gets_one_server_side_terminal_line(tmp_path, monkeypatch):
    path = _enable(tmp_path, monkeypatch)
    manager = _manager()
    sess = _session(manager)

    def _replace(_stream):
        replacement = ssh_manager._Session(sess.device, MARKER, "owner-2", sess.device_id)
        replacement.state = "connected"
        replacement.generation = 2
        manager.sessions[sess.device_id] = replacement

    sess.client = _FakeClient(_FakeChannel([("out", f"{MARKER}\n".encode())], on_event=_replace))
    raw = f"echo {MARKER}"
    manager._exec(sess, raw, f"label {MARKER}", False, raw_command_length=len(raw))

    content = open(path, encoding="utf-8").read()
    assert MARKER not in content
    _assert_one_run(
        path,
        f"SSH [device-7] command started (sudo: no, {len(raw)} chars)",
        "SSH [device-7] command ended: session replaced or disconnected",
    )


@pytest.mark.parametrize("raw", ["  apt list --upgradable  ", "sudo apt update"])
def test_run_command_passes_the_submitted_length_not_the_sudo_wrapped_one(monkeypatch, raw):
    manager = _manager()
    sess = _session(manager)
    sess.client = object()
    started = []

    class _CapturedThread:
        def __init__(self, target, args, daemon):
            started.append(args)

        def start(self):
            return None

    monkeypatch.setattr(ssh_manager.threading, "Thread", _CapturedThread)

    assert manager.run_command("device-7", raw, True, owner="owner-1") == {"ok": True}

    wrapped_cmd, raw_length = started[0][1], started[0][-1]
    assert raw_length == len(raw.strip())
    assert len(wrapped_cmd) > raw_length


def test_broadcast_skips_ssh_log_debug_writes_and_preserves_delivery(tmp_path, monkeypatch):
    path = _enable(tmp_path, monkeypatch)
    delivered = []

    async def _broadcast(message):
        delivered.append(message)

    monkeypatch.setattr(main.ws_mgr, "broadcast", _broadcast)
    message = {"type": "ssh_log", "device_id": "device-7", "level": "out", "text": MARKER}

    asyncio.run(main._broadcast(message))

    assert delivered == [message]
    assert MARKER not in open(path, encoding="utf-8").read()
