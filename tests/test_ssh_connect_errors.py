"""Tests that a failed SSH connect shows a fixed, plain-language message per
failure type and logs only the error type, never the error's own text.

`_Session.connect` is monkeypatched to raise, so nothing here touches the
network."""

import errno
import socket

import paramiko
import pytest

import ssh_manager

MARKER = "raw-remote-error-marker"

_NAME = "Could not connect: the device's address could not be found. Check the host name."
_TIMEOUT = "Could not connect: the device did not respond in time. Check that it is on and reachable."
_REFUSED = "Could not connect: the device refused the connection. Check that SSH is running on it."
_PROTOCOL = "Could not connect: the SSH handshake with the device failed."
_OTHER = "Could not connect. Check that the device is on and reachable."


def _no_valid(*errnos):
    errors = {(f"10.0.0.{i}", 22): OSError(code, MARKER) for i, code in enumerate(errnos)}
    return paramiko.ssh_exception.NoValidConnectionsError(errors)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (socket.gaierror(socket.EAI_NONAME, MARKER), _NAME),
        (TimeoutError(MARKER), _TIMEOUT),
        (ConnectionRefusedError(errno.ECONNREFUSED, MARKER), _REFUSED),
        (_no_valid(errno.ECONNREFUSED), _REFUSED),
        (_no_valid(errno.ECONNREFUSED, errno.ECONNREFUSED), _REFUSED),
        (_no_valid(errno.ECONNREFUSED, errno.EHOSTUNREACH), _OTHER),
        (_no_valid(errno.EHOSTUNREACH), _OTHER),
        (paramiko.SSHException(MARKER), _PROTOCOL),
        (EOFError(MARKER), _OTHER),
        (OSError(errno.ENETUNREACH, MARKER), _OTHER),
        (RuntimeError(MARKER), _OTHER),
    ],
)
def test_connect_error_message_classifies_failures(exc, expected):
    assert ssh_manager._connect_error_message(exc) == expected


def test_failed_connect_returns_fixed_message_and_logs_type_only(monkeypatch):
    debug = []

    async def _noop(_msg):
        pass

    mgr = ssh_manager.SSHManager(_noop, debug.append)
    device = {"id": "d1", "name": "Pi", "host": "10.0.0.9", "username": "pi"}

    def boom(self):
        raise paramiko.SSHException(MARKER)

    monkeypatch.setattr(ssh_manager._Session, "connect", boom)
    result = mgr.connect("d1", "pw", device, "owner-1")

    assert result == {"ok": False, "error": _PROTOCOL}
    assert "d1" not in mgr.sessions
    assert debug == ['SSH [d1 "Pi" 10.0.0.9] connect failed (SSHException)']
