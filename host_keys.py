"""Approved SSH host keys: the known_hosts file, its private atomic save,
and the trust-on-first-use prompt pieces."""

import base64
import hashlib
import os
import tempfile
import time

import paramiko


# Shares SND_DATA_DIR with main.py so known_hosts stays with the rest of the data.
DATA_DIR         = os.environ.get("SND_DATA_DIR", "/var/lib/simple-network-dashboard")
KNOWN_HOSTS_FILE = os.path.join(DATA_DIR, "known_hosts")

def _fp(key) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _load_known_hosts() -> paramiko.HostKeys:
    hk = paramiko.HostKeys()
    try:
        hk.load(KNOWN_HOSTS_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:
        raise KnownHostsUnreadable() from e
    return hk


def _save_known_hosts(host_keys: paramiko.HostKeys) -> None:
    folder = os.path.dirname(KNOWN_HOSTS_FILE) or "."
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix=".known_hosts-", suffix=".tmp", dir=folder)
        try:
            if os.name != "nt" and hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        host_keys.save(temp_path)
        with open(temp_path, "rb+") as f:
            os.fsync(f.fileno())
        os.replace(temp_path, KNOWN_HOSTS_FILE)
        temp_path = None
        if os.name != "nt":
            directory_fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


class KnownHostsUnreadable(Exception):
    """The existing approved host-key file could not be read safely."""


class UnknownHostKey(Exception):
    def __init__(self, hostname, key):
        super().__init__("unknown host key")
        self.hostname = hostname
        self.key      = key


class _TofuPolicy(paramiko.MissingHostKeyPolicy):
    """Trust-on-first-use: surface the offered key instead of auto-accepting."""
    def missing_host_key(self, client, hostname, key):
        raise UnknownHostKey(hostname, key)


class _PendingHostKey:
    """One device's outstanding host-key prompt: the offered key, a one-time
    code the browser that raised the prompt must echo back, the owner
    (signed-in session) that raised it, and the reserved session's generation the
    prompt is bound to, so a later connect/disconnect on the same device can
    never be resolved by an accept/reject aimed at a stale prompt."""

    def __init__(self, host, key, code: str, owner: str, generation: int):
        self.host       = host
        self.key        = key
        self.code       = code
        self.owner      = owner
        self.generation = generation
        self.created_at = time.monotonic()
