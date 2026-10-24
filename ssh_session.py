"""One SSH session slot for a device, and plain-language connect errors."""

import errno
import socket
import threading
import time

import paramiko

import host_keys
from host_keys import KnownHostsUnreadable, _TofuPolicy


def _connect_error_message(exc: BaseException) -> str:
    """Turn a connect failure into a fixed, plain-language message for the
    browser. The exception's own text is never shown: it can quote the remote
    device or the SSH library's internals.

    socket.gaierror and TimeoutError are OSError subclasses, so they are
    checked before the refused cases. paramiko raises NoValidConnectionsError
    when every address refused the connection or was unreachable; it counts
    as refused only when every address refused."""
    if isinstance(exc, socket.gaierror):
        return "Could not connect: the device's address could not be found. Check the host name."
    if isinstance(exc, TimeoutError):
        return "Could not connect: the device did not respond in time. Check that it is on and reachable."
    refused = isinstance(exc, ConnectionRefusedError)
    if isinstance(exc, paramiko.ssh_exception.NoValidConnectionsError):
        refused = bool(exc.errors) and all(
            getattr(e, "errno", None) == errno.ECONNREFUSED for e in exc.errors.values())
    if refused:
        return "Could not connect: the device refused the connection. Check that SSH is running on it."
    if isinstance(exc, paramiko.SSHException):
        return "Could not connect: the SSH handshake with the device failed."
    return "Could not connect. Check that the device is on and reachable."


class _Session:
    """One SSH session slot for a device.

    `state` is one of "connecting", "connected", "host_key_pending", or
    "closed" and doubles as the reservation: a session is placed into the
    manager's `sessions` dict as soon as a connect attempt begins, before the
    network connect runs, so a second concurrent connect for the same device
    sees the reservation instead of an empty slot.

    `generation` is a manager-assigned, monotonically increasing number used
    (from a later phase) to drop stale worker output that was queued for
    delivery before a disconnect/reconnect replaced this session.
    """

    def __init__(self, device: dict, password: str, owner: str = None, device_id: str = None):
        self.device           = device
        self.device_id         = device_id
        self.password          = password   # memory-only
        self.client            = None
        self.busy              = False
        self.channel           = None
        self.run_id            = None
        self.cancelled         = False
        self.cancel_requested  = False
        self.owner             = owner
        self.state             = "connecting"
        self.generation        = 0
        self.last_active       = time.monotonic()
        self.last_io           = self.last_active
        self.writer            = None
        self.controller        = None
        self.control_rev       = 0
        self.auth_complete     = False
        self.cols              = None
        self.rows              = None
        self.stop_timer        = None
        self.forced            = False
        self.writer_stuck      = False
        self.history           = None
        self.label             = None
        self.sent_seq          = 0
        self.sent_bytes        = {}
        self.acked_seq         = 0
        self.ack_progress_at   = time.monotonic()
        self.ack_stalled       = False
        self.term_condition    = threading.Condition()

    def connect(self):
        c = paramiko.SSHClient()
        try:
            c.load_host_keys(host_keys.KNOWN_HOSTS_FILE)
        except FileNotFoundError:
            pass
        except Exception as e:
            raise KnownHostsUnreadable() from e
        c.set_missing_host_key_policy(_TofuPolicy())
        c.connect(
            hostname=self.device["host"],
            username=self.device["username"],
            password=self.password,
            timeout=12,
            allow_agent=False,
            look_for_keys=False,
        )
        self.client = c
        try:
            t = c.get_transport()
            if t:
                t.set_keepalive(30)
        except Exception:
            pass

    def close(self):
        try:
            if self.client:
                self.client.close()
        finally:
            self.client   = None
            self.channel  = None
            self.password = None   # wipe from memory
            self.history = None
