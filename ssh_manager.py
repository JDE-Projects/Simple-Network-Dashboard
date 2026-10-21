"""
SSH session management — ported and adapted from Simple SSH Tool.
Passwords are held in memory only; never written anywhere.
"""

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import tempfile
import threading
import time

import paramiko

from delivery_queue import DeliveryQueue
from terminal_history import TerminalHistory, trim_to_budget
from terminal_run import OutputBatcher, SudoGate, TerminalWriter, build_command, clamp_size, read_channel
import config

IDLE_WARN_SECONDS    = 270   # send warning after 4.5 minutes idle
IDLE_TIMEOUT_SECONDS = 300   # disconnect after 5 minutes idle

HOST_KEY_PENDING_SECONDS = 300   # a host-key prompt expires 5 minutes after it is raised

_EXPIRED_ERROR = "This host-key prompt expired or was already used. Reconnect to try again."

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

    def connect(self):
        c = paramiko.SSHClient()
        try:
            c.load_host_keys(KNOWN_HOSTS_FILE)
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


# ---------------------------------------------------------------------------
# Public manager — one instance shared across the whole app
# ---------------------------------------------------------------------------

class SSHManager:
    def __init__(self, broadcast_fn, debug_write=None):
        """broadcast_fn: async coroutine function that pushes a dict to all WS clients.
        debug_write: optional callable(str) used to log internal exception detail
        that must not reach the browser; defaults to a no-op."""
        self._broadcast   = broadcast_fn
        self._debug_write = debug_write if debug_write is not None else (lambda _text: None)
        self._delivery  = DeliveryQueue(lambda msg: self._deliver(msg), self._debug_write)
        self.sessions: dict[str, _Session] = {}
        self._pending: dict[str, _PendingHostKey] = {}  # device_id -> pending prompt
        self._owner_activity: dict[str, dict] = {}  # owner -> {"last_active": float, "warned": bool}

        # Per-device locks, created on demand under a short guard lock. Every
        # state transition on a device's session (reserve, promote, retire,
        # close, command reservation, cancel bookkeeping) runs inside the
        # one device lock for that device. Locks are always taken singly,
        # never nested, and never held across a blocking call.
        self._dlocks:       dict[str, threading.Lock] = {}
        self._dlocks_guard  = threading.Lock()

        # _owner_activity is read-modified-written from the worker thread,
        # the idle loop, and the connect executor thread; it gets its own
        # guard so a read-modify-write never races another thread's.
        self._owner_activity_lock = threading.Lock()

        self._gen_lock    = threading.Lock()
        self._gen_counter = 0

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._delivery.start(loop)

    # ---- locking helpers ---------------------------------------------------

    def _device_lock(self, device_id: str) -> threading.Lock:
        """Get-or-create the lock for one device. The guard is held only for
        this lookup, never across real work."""
        with self._dlocks_guard:
            lock = self._dlocks.get(device_id)
            if lock is None:
                lock = threading.Lock()
                self._dlocks[device_id] = lock
            return lock

    def _next_generation(self) -> int:
        with self._gen_lock:
            self._gen_counter += 1
            return self._gen_counter

    def _is_current(self, device_id: str, sess: "_Session") -> bool:
        """Cheap identity check for the fast path: is `sess` still the
        session in the slot for this device? A plain dict read, never taken
        under the device lock, so it never blocks on another thread."""
        return self.sessions.get(device_id) is sess

    # ---- owner-activity helpers (single lock, every read-modify-write) ----

    def _owner_touch(self, owner: str) -> None:
        with self._owner_activity_lock:
            self._owner_activity[owner] = {"last_active": time.monotonic(), "warned": False}

    def _owner_reset(self, owner: str) -> None:
        with self._owner_activity_lock:
            info = self._owner_activity.get(owner)
            if info:
                info["last_active"] = time.monotonic()
                info["warned"] = False

    def _owner_pop(self, owner: str) -> None:
        with self._owner_activity_lock:
            self._owner_activity.pop(owner, None)

    # ---- internal push helpers (safe to call from any thread) -------------

    def _push(self, msg: dict):
        """Queue a non-terminal message without blocking its caller."""
        self._delivery.push(msg)

    def _push_terminal(self, msg: dict, should_continue) -> bool:
        """Queue terminal output, waiting for capacity. Never call this while
        holding a device lock (see DeliveryQueue.push_terminal)."""
        return self._delivery.push_terminal(msg, should_continue)

    def shutdown_delivery(self):
        """Wake terminal producers and stop the event-loop delivery drain."""
        self._delivery.shutdown()

    async def _deliver(self, msg: dict):
        """Runs on the event loop thread. `_push` only schedules delivery, so
        a worker-originated message can still be sitting in the loop's queue
        after a disconnect/reconnect replaces its session. A message stamped
        with the internal "_gen" key is dropped here, at delivery time, if
        the device's current session generation no longer matches the one it
        was stamped with. The internal key is always stripped before the
        message reaches `_broadcast`; a message with no "_gen" key passes
        through unchanged."""
        gen = msg.pop("_gen", None)
        if gen is not None:
            device_id = msg.get("device_id")
            current = self.sessions.get(device_id)
            if current is None or current.generation != gen:
                return
            msg["_still_current"] = lambda: (
                (current := self.sessions.get(device_id)) is not None
                and current.generation == gen
            )
        await self._broadcast(msg)

    def _log(self, device_id: str, text: str, level: str = "out", owner: str = None,
              cmd_id: str = None, gen: int = None):
        msg = {"type": "ssh_log", "device_id": device_id, "text": text, "level": level}
        if cmd_id is not None:
            msg["cmd_id"] = cmd_id
        if owner is None:
            sess = self.sessions.get(device_id)
            if sess:
                owner = sess.owner
        if owner:
            msg["_owner"] = owner
        if gen is not None:
            msg["_gen"] = gen
        self._push(msg)

    def _status(self, device_id: str, state: str, owner: str = None, gen: int = None):
        msg = {"type": "ssh_status", "device_id": device_id, "state": state}
        if owner is None:
            sess = self.sessions.get(device_id)
            if sess:
                owner = sess.owner
        if owner:
            msg["_owner"] = owner
        if gen is not None:
            msg["_gen"] = gen
        self._push(msg)

    def _lock(self, device_id: str, locked: bool, gen: int = None):
        """Broadcast lock signal to everyone (no _owner key)."""
        msg = {"type": "ssh_lock", "device_id": device_id, "locked": locked}
        if gen is not None:
            msg["_gen"] = gen
        self._push(msg)

    # ---- host-key helpers -------------------------------------------------

    def get_host_key(self, host: str) -> dict:
        try:
            sub = _load_known_hosts().lookup(host) if host else None
        except KnownHostsUnreadable as e:
            self._debug_write(f"get_host_key failed: {type(e.__cause__).__name__}: {e.__cause__}")
            return {"ok": False,
                    "error": "The approved host keys file could not be read. Check the server log."}
        if not sub:
            return {"known": False, "host": host}
        return {"known": True, "host": host,
                "entries": [{"key_type": kt, "fingerprint": _fp(k)} for kt, k in sub.items()]}

    def _clear_pending(self, device_id: str) -> None:
        """Drop a device's outstanding host-key prompt, if any. Safe to call
        from any teardown path; a no-op when nothing is pending."""
        with self._device_lock(device_id):
            self._pending.pop(device_id, None)

    def _valid_host_key_pending(self, device_id: str, code: str, owner: str):
        """Must be called under the device's lock. Returns the pending
        record only when it is still bound to the slot's current session,
        was raised by this owner, the code matches (constant-time), and it
        has not expired; otherwise returns None and leaves `_pending`
        untouched."""
        pending = self._pending.get(device_id)
        if pending is None:
            return None
        sess = self.sessions.get(device_id)
        if (sess is None
                or sess.generation != pending.generation
                or sess.state != "host_key_pending"
                or pending.owner != owner
                or not hmac.compare_digest(pending.code, code or "")
                or (time.monotonic() - pending.created_at) > HOST_KEY_PENDING_SECONDS):
            return None
        return pending

    def trust_host_key(self, device_id: str, code: str, owner: str) -> dict:
        with self._device_lock(device_id):
            pending = self._valid_host_key_pending(device_id, code, owner)
            if pending is None:
                return {"ok": False, "expired": True, "error": _EXPIRED_ERROR}
            self._pending.pop(device_id, None)
            host, key = pending.host, pending.key
            bound_sess = self.sessions.get(device_id)

        try:
            hk = _load_known_hosts()
            if hk.lookup(host):
                del hk[host]
            hk.add(host, key.get_name(), key)
            _save_known_hosts(hk)
            return {"ok": True, "host": host, "fingerprint": _fp(key)}
        except KnownHostsUnreadable as e:
            self._debug_write(f"trust_host_key failed: {type(e.__cause__).__name__}: {e.__cause__}")
            error = ("The approved host keys file could not be read, so nothing was changed. "
                     "Check the server log.")
        except Exception as e:
            self._debug_write(f"trust_host_key failed: {type(e).__name__}: {e}")
            error = "Could not save the host key."

        self._log(device_id, "Host key approval failed. Password cleared from memory.", "muted", pending.owner)
        self._close(device_id, bound_sess)
        return {"ok": False, "error": error}

    def reject_host_key(self, device_id: str, code: str, owner: str) -> dict:
        with self._device_lock(device_id):
            pending = self._valid_host_key_pending(device_id, code, owner)
            if pending is None:
                return {"ok": False, "expired": True, "error": _EXPIRED_ERROR}
            self._pending.pop(device_id, None)
            bound_sess = self.sessions.get(device_id)

        self._log(device_id, "Host key declined. Password cleared from memory.", "muted", pending.owner)
        self._close(device_id, bound_sess)
        return {"ok": True}

    def forget_host_key(self, host: str) -> dict:
        try:
            hk = _load_known_hosts()
            if hk.lookup(host):
                del hk[host]
                _save_known_hosts(hk)
            return {"ok": True}
        except KnownHostsUnreadable as e:
            self._debug_write(f"forget_host_key failed: {type(e.__cause__).__name__}: {e.__cause__}")
            return {"ok": False,
                    "error": ("The approved host keys file could not be read, so nothing was changed. "
                              "Check the server log.")}
        except Exception as e:
            self._debug_write(f"forget_host_key failed: {type(e).__name__}: {e}")
            return {"ok": False, "error": "Could not forget the host key."}

    # ---- connection -------------------------------------------------------

    def connect(self, device_id: str, password: str, device: dict, owner: str) -> dict:
        """Synchronous — run via run_in_executor from the async route handler."""
        with self._device_lock(device_id):
            existing = self.sessions.get(device_id)
            old_sess = None
            if existing and existing.state in ("connecting", "connected", "host_key_pending"):
                if existing.owner != owner:
                    return {"ok": False, "in_use": True,
                            "error": "This device is in use by another session."}
                # Same owner replacing its own session: retire it in the same
                # locked section that reserves the new slot, so no other
                # thread can ever observe the slot empty or holding both.
                existing.state = "closed"
                existing.cancelled = True
                existing.cancel_requested = True
                old_sess = existing

            sess = _Session(device, password, owner, device_id)
            sess.state = "connecting"
            sess.generation = self._next_generation()
            self.sessions[device_id] = sess

        if old_sess is not None:
            old_sess.close()   # blocking network close, done outside the lock
            self._log(device_id, "Disconnected. Password cleared from memory.", "muted", owner)

        # The blocking network connect (~up to 12s) runs with no lock held.
        try:
            sess.connect()
        except UnknownHostKey as e:
            code = secrets.token_urlsafe(24)
            with self._device_lock(device_id):
                if self.sessions.get(device_id) is sess:
                    sess.state = "host_key_pending"
                    self._pending[device_id] = _PendingHostKey(
                        e.hostname, e.key, code, owner, sess.generation)
            return {
                "ok": False, "host_key_unknown": True,
                "host": device["host"], "key_type": e.key.get_name(),
                "fingerprint": _fp(e.key), "code": code,
            }
        except paramiko.BadHostKeyException as e:
            code = secrets.token_urlsafe(24)
            with self._device_lock(device_id):
                if self.sessions.get(device_id) is sess:
                    sess.state = "host_key_pending"
                    self._pending[device_id] = _PendingHostKey(
                        device["host"], e.key, code, owner, sess.generation)
            return {
                "ok": False, "host_key_changed": True,
                "host": device["host"], "key_type": e.key.get_name(),
                "new_fingerprint": _fp(e.key),
                "old_fingerprint": _fp(e.expected_key),
                "code": code,
            }
        except paramiko.AuthenticationException:
            with self._device_lock(device_id):
                if self.sessions.get(device_id) is sess:
                    del self.sessions[device_id]
                    removed = True
                else:
                    removed = False
            if removed and old_sess is not None:
                self._status(device_id, "idle", owner)
                self._lock(device_id, False)
            return {"ok": False, "error": "Authentication failed. Check username and password."}
        except KnownHostsUnreadable as e:
            with self._device_lock(device_id):
                if self.sessions.get(device_id) is sess:
                    del self.sessions[device_id]
                    removed = True
                else:
                    removed = False
            if removed and old_sess is not None:
                self._status(device_id, "idle", owner)
                self._lock(device_id, False)
            self._debug_write(f"connect failed: {type(e.__cause__).__name__}: {e.__cause__}")
            return {"ok": False,
                    "error": ("Could not connect: the approved host keys file could not be read. "
                              "Check the server log.")}
        except Exception as e:
            with self._device_lock(device_id):
                if self.sessions.get(device_id) is sess:
                    del self.sessions[device_id]
                    removed = True
                else:
                    removed = False
            if removed and old_sess is not None:
                self._status(device_id, "idle", owner)
                self._lock(device_id, False)
            return {"ok": False, "error": f"Could not connect: {e}"}

        with self._device_lock(device_id):
            if self.sessions.get(device_id) is not sess:
                superseded = True
            else:
                sess.state = "connected"
                self._pending.pop(device_id, None)
                superseded = False

        if superseded:
            # A newer connect (same owner) won the race while we were
            # connecting; discard the socket we just made.
            sess.close()
            return {"ok": False, "superseded": True,
                    "error": "This device was reconnected from another tab."}

        self._owner_touch(owner)
        self._log(device_id, f"Connected to {device['host']} as {device['username']}.",
                   "ok", owner, gen=sess.generation)
        self._status(device_id, "connected", owner, gen=sess.generation)
        self._lock(device_id, True, gen=sess.generation)
        return {"ok": True}

    def _close(self, device_id: str, sess: "_Session" = None):
        """Identity-checked close: pop `device_id`'s slot only if it still
        holds `sess` (or holds anything at all, when `sess` is None), then
        close the socket outside the lock."""
        with self._device_lock(device_id):
            current = self.sessions.get(device_id)
            if current is None or (sess is not None and current is not sess):
                popped = None
            else:
                del self.sessions[device_id]
                current.state = "closed"
                current.cancelled = True
                current.cancel_requested = True
                popped = current

        # Only announce idle/unlock when this call actually tore a session
        # down. A stale, identity-mismatched close (a newer session already
        # holds the slot) must not broadcast idle or unlock, or it would wipe
        # the live replacement's lock and status in every browser.
        if popped is not None:
            self._clear_pending(device_id)
            owner = popped.owner
            popped.close()
            self._log(device_id, "Disconnected. Password cleared from memory.", "muted", owner)
            self._status(device_id, "idle", owner)
            self._lock(device_id, False)

    def disconnect(self, device_id: str, owner: str = None) -> dict:
        # Owner check and pop are one locked section so a superseding connect
        # (or a race with another close) can never slip between them.
        with self._device_lock(device_id):
            sess = self.sessions.get(device_id)
            if owner is not None and sess is not None and sess.owner != owner:
                return {"ok": False, "not_owner": True,
                        "error": "This device is in use by another session."}
            popped = sess
            if popped is not None:
                del self.sessions[device_id]
                popped.state = "closed"
                popped.cancelled = True
                popped.cancel_requested = True

        broadcast_owner = owner if owner is not None else (popped.owner if popped else None)
        if popped is not None:
            self._clear_pending(device_id)
            popped.close()
            self._log(device_id, "Disconnected. Password cleared from memory.", "muted", popped.owner)
        self._status(device_id, "idle", broadcast_owner)
        self._lock(device_id, False)
        return {"ok": True}

    def disconnect_all(self) -> dict:
        for did in list(self.sessions):
            self._close(did)
        with self._owner_activity_lock:
            self._owner_activity.clear()
        return {"ok": True}

    def claim_terminal(self, device_id: str, run_id: str, owner: str, socket_id: str) -> dict:
        with self._device_lock(device_id):
            sess = self.sessions.get(device_id)
            if not sess or sess.owner != owner or sess.state != "connected" or not sess.busy or sess.run_id != run_id:
                self._debug_write(f"SSH [{device_id}] terminal input refused")
                return {"ok": False}
            sess.controller = socket_id
            sess.control_rev += 1
            if sess.writer:
                sess.writer.clear_input()
            return {"ok": True, "rev": sess.control_rev}

    def terminal_input(self, device_id, run_id, rev, data, owner, socket_id) -> dict:
        with self._device_lock(device_id):
            sess = self.sessions.get(device_id)
            valid = (isinstance(data, str) and len(data) <= config.TERM_INPUT_MAX_CHARS and sess
                     and sess.owner == owner and sess.state == "connected" and sess.busy
                     and sess.run_id == run_id and sess.controller == socket_id
                     and sess.control_rev == rev and sess.auth_complete and sess.writer is not None)
            if not valid:
                self._debug_write(f"SSH [{device_id}] terminal input refused")
                return {"ok": False}
            if not sess.writer.input(data):
                return {"ok": False, "full": True}
            sess.last_io = time.monotonic()
            return {"ok": True}

    def terminal_resize(self, device_id, run_id, rev, cols, rows, owner, socket_id) -> dict:
        with self._device_lock(device_id):
            sess = self.sessions.get(device_id)
            valid = (isinstance(cols, int) and not isinstance(cols, bool) and isinstance(rows, int) and not isinstance(rows, bool)
                     and sess and sess.owner == owner and sess.state == "connected" and sess.busy
                     and sess.run_id == run_id and sess.controller == socket_id and sess.control_rev == rev
                     and sess.auth_complete and sess.writer is not None)
            if not valid:
                self._debug_write(f"SSH [{device_id}] terminal input refused")
                return {"ok": False}
            sess.cols, sess.rows = clamp_size(cols, rows)
            sess.writer.resize(sess.cols, sess.rows)
            return {"ok": True}

    def cancel(self, device_id: str, run_id: str = None, owner: str = None) -> dict:
        with self._device_lock(device_id):
            sess = self.sessions.get(device_id)
            if not sess or not sess.busy or sess.run_id != run_id:
                return {"ok": False}
            if owner is not None and sess.owner != owner:
                return {"ok": False, "not_owner": True,
                        "error": "This device is in use by another session."}
            # `cancel_requested` is honored by _exec even before sess.channel
            # is wired up, so a cancel arriving in that early window still
            # takes effect instead of being silently dropped.
            sess.cancelled = True
            sess.cancel_requested = True
            channel = sess.channel
            ready = channel is not None and sess.auth_complete and sess.writer is not None
            if ready and sess.stop_timer is not None:
                sess.forced = True
                channel.close()
                return {"ok": True}
            if ready:
                sess.writer.interrupt()
                timer = threading.Timer(config.TERM_STOP_GRACE_SECONDS, self._force_stop,
                                        args=(device_id, sess, run_id, channel))
                timer.daemon = True
                sess.stop_timer = timer
                timer.start()
                return {"ok": True}
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass
        return {"ok": True}

    def _force_stop(self, device_id, sess, run_id, channel):
        with self._device_lock(device_id):
            if self.sessions.get(device_id) is not sess or sess.run_id != run_id or sess.channel is not channel:
                return
            sess.forced = True
            channel.close()

    # ---- command execution ------------------------------------------------

    def run_command(self, device_id: str, raw_cmd: str, use_sudo: bool,
                    label: str = None, owner: str = None, cmd_id: str = None,
                    cols=None, rows=None) -> dict:
        label = label or "Custom command"
        cols, rows = clamp_size(cols, rows)
        with self._device_lock(device_id):
            sess = self.sessions.get(device_id)
            if not sess or sess.state != "connected":
                return {"ok": False, "error": "Not connected."}
            if owner is not None and sess.owner != owner:
                return {"ok": False, "not_owner": True,
                        "error": "This device is in use by another session."}
            raw_cmd = (raw_cmd or "").strip()
            if not raw_cmd:
                return {"ok": False, "error": "Empty command."}
            if sess.busy:
                return {"ok": False, "error": "A command is already running."}
            # Reserve the run before the worker thread even starts, so a
            # second run started right after this one cannot both pass the
            # busy check.
            sess.busy = True
            sess.cancelled = False
            sess.cancel_requested = False
            sess.forced = False
            sess.writer_stuck = False
            sess.stop_timer = None
            sess.run_id = secrets.token_urlsafe(16)
            sess.controller = None
            sess.control_rev = 0
            sess.auth_complete = False
            sess.last_io = time.monotonic()
            sess.history = TerminalHistory()
            sess.label = label
            sess.cols, sess.rows = cols, rows
            run_id = sess.run_id

        cmd, phrase, mark = build_command(raw_cmd, use_sudo)

        threading.Thread(
            target=self._exec,
            args=(sess, cmd, label, use_sudo, cmd_id, phrase, mark, cols, rows, len(raw_cmd), run_id),
            daemon=True,
        ).start()
        return {"ok": True, "run_id": run_id}

    def _exec(self, sess: "_Session", cmd: str, label: str, feed_sudo: bool,
              cmd_id: str = None, sudo_phrase: str = None, sudo_mark: str = None,
              cols: int = None, rows: int = None, raw_command_length: int = 0,
              run_id: str = None):
        device_id = sess.device_id
        # Captured once: this worker may emit or change state only while it
        # remains current. Every emit below is stamped with this generation
        # too, so a message already queued for delivery is dropped if a
        # disconnect/reconnect replaces the session before it is delivered.
        gen = sess.generation

        if not sess.client:
            self._debug_write(f"SSH [{device_id}] command not started: not connected")
            if self._is_current(device_id, sess):
                self._log(device_id, "Not connected.", "err", gen=gen)
            self._finish_exec(sess)
            return

        self._debug_write(
            f"SSH [{device_id}] command started (sudo: {'yes' if feed_sudo else 'no'}, "
            f"{raw_command_length} chars)"
        )
        debug_end = None
        run_id = run_id or sess.run_id
        cols, rows = clamp_size(cols, rows)

        if self._is_current(device_id, sess):
            self._status(device_id, "running", gen=gen)
            self._log(device_id, f"$ {label}", "cmd", cmd_id=cmd_id, gen=gen)

        if sess.cancel_requested:
            # A cancel arrived before the channel even existed; honor it
            # without ever starting the command.
            sess.cancelled = True
            if self._is_current(device_id, sess):
                self._log(device_id, f"■ {label} cancelled.", "warn", gen=gen)
            debug_end = f"SSH [{device_id}] command cancelled"
            self._debug_write(debug_end)
            self._finish_exec(sess)
            return

        try:
            transport = sess.client.get_transport()
            chan = transport.open_session()
            chan.get_pty(term="xterm-256color", width=cols, height=rows)
            chan.exec_command(cmd)
            sess.channel = chan
            sess.cols, sess.rows = cols, rows
            writer = TerminalWriter(chan, lambda: self._writer_stuck(device_id, sess, run_id, chan))
            sess.writer = writer
            writer.start()
            if sess.cancel_requested:
                sess.cancelled = True
                try:
                    chan.close()
                except Exception:
                    pass

            gate = SudoGate(sudo_phrase, sudo_mark) if feed_sudo else None
            started = False

            def send_start():
                nonlocal started
                if not started and self._is_current(device_id, sess):
                    started = True
                    sess.auth_complete = True
                    self._push({"type": "term_start", "device_id": device_id, "run_id": run_id,
                                "cmd_id": cmd_id, "cols": cols, "rows": rows, "_owner": sess.owner,
                                "_gen": gen})

            def emit(text, seq):
                send_start()
                with self._device_lock(device_id):
                    if self.sessions.get(device_id) is not sess or sess.run_id != run_id:
                        return False
                    if sess.history is not None:
                        sess.history.feed(text, seq)
                    sess.last_io = time.monotonic()
                return self._push_terminal({"type": "term_data", "device_id": device_id,
                    "run_id": run_id, "seq": seq, "data": text, "_owner": sess.owner, "_gen": gen},
                    lambda: self._is_current(device_id, sess) and sess.run_id == run_id)

            if not feed_sudo:
                send_start()
            result = read_channel(chan, gate, OutputBatcher(), emit,
                lambda: self._is_current(device_id, sess) and sess.run_id == run_id,
                lambda: self._queue_sudo_password(writer, sess.password), send_start)

            if result.superseded or not self._is_current(device_id, sess):
                debug_end = f"SSH [{device_id}] command ended: session replaced or disconnected"
                return

            if result.gate_passed:
                send_start()
            if started:
                self._push({"type": "term_end", "device_id": device_id, "run_id": run_id,
                            "_owner": sess.owner, "_gen": gen})

            if sess.cancelled:
                if sess.forced:
                    self._log(device_id, f"■ {label} did not respond to Ctrl+C, so it was stopped.", "warn", gen=gen)
                else:
                    self._log(device_id, f"■ {label} cancelled.", "warn", gen=gen)
                debug_end = (f"SSH [{device_id}] command stopped: device stopped accepting input"
                             if sess.writer_stuck else f"SSH [{device_id}] command cancelled")
            elif result.refused:
                self._log(device_id, f"✗ {label} did not run: sudo did not accept the password.", "err", gen=gen)
                debug_end = f"SSH [{device_id}] command stopped: sudo did not accept the password"
            elif not result.gate_passed:
                self._log(device_id, f"✗ {label} did not run.", "err", gen=gen)
                debug_end = f"SSH [{device_id}] command did not run"
            else:
                if result.exit_code == 0:
                    self._log(device_id, f"✓ {label} finished.", "ok", gen=gen)
                else:
                    self._log(device_id, f"✗ {label} exited with code {result.exit_code}.", "err", gen=gen)
                debug_end = f"SSH [{device_id}] command finished (exit code {result.exit_code})"

        except Exception as e:
            if not self._is_current(device_id, sess):
                debug_end = f"SSH [{device_id}] command ended: session replaced or disconnected"
            elif sess.cancelled:
                if sess.forced:
                    self._log(device_id, f"■ {label} did not respond to Ctrl+C, so it was stopped.", "warn", gen=gen)
                else:
                    self._log(device_id, f"■ {label} cancelled.", "warn", gen=gen)
                debug_end = (f"SSH [{device_id}] command stopped: device stopped accepting input"
                             if sess.writer_stuck else f"SSH [{device_id}] command cancelled")
            else:
                self._log(device_id, f"Error: {e}", "err", gen=gen)
                debug_end = f"SSH [{device_id}] command failed ({type(e).__name__})"
        finally:
            if debug_end is None:
                debug_end = f"SSH [{device_id}] command ended: session replaced or disconnected"
            self._debug_write(debug_end)
            self._finish_exec(sess)

    def _writer_stuck(self, device_id, sess, run_id, channel):
        with self._device_lock(device_id):
            if self.sessions.get(device_id) is sess and sess.run_id == run_id and sess.channel is channel:
                sess.cancelled = True
                sess.forced = True
                sess.writer_stuck = True
                channel.close()

    @staticmethod
    def _queue_sudo_password(writer, password):
        done = writer.password(password)
        if done is not None:
            done.wait(config.TERM_WRITE_DEADLINE)

    def _finish_exec(self, sess: "_Session"):
        """Identity-checked cleanup: clear busy/channel, reset the idle
        clock, and emit the trailing "connected" status only if this session
        is still the current one for its device."""
        device_id = sess.device_id
        writer = None
        with self._device_lock(device_id):
            writer = sess.writer
            if writer:
                writer.close()
            if sess.stop_timer:
                sess.stop_timer.cancel()
                sess.stop_timer = None
            if self.sessions.get(device_id) is sess:
                sess.busy    = False
                sess.channel = None
                sess.run_id  = None
                sess.writer = None
                sess.history = None
                sess.label = None
                owner = sess.owner
                still_current = True
            else:
                owner = None
                still_current = False

        if writer:
            writer.thread.join(config.TERM_SEND_TIMEOUT + 0.1)

        if still_current:
            if owner:
                self._owner_reset(owner)
            # Stamped with this session's generation: the identity check
            # above only guards against a replace that already happened; a
            # replace landing between here and delivery is caught at
            # delivery time by the generation stamp.
            self._status(device_id, "connected", gen=sess.generation)

    def owner_terminal_snapshot(self, owner: str) -> list[dict]:
        """Return recoverable terminal state for this owner's current runs."""
        terminals = []
        for device_id in list(self.sessions):
            with self._device_lock(device_id):
                sess = self.sessions.get(device_id)
                if (sess is None or sess.owner != owner or sess.state != "connected"
                        or not sess.busy or sess.history is None):
                    continue
                terminals.append({
                    "device_id": device_id,
                    "run_id": sess.run_id,
                    "cols": sess.cols,
                    "rows": sess.rows,
                    "label": sess.label,
                    **sess.history.snapshot(),
                })
        return trim_to_budget(terminals, config.TERM_INIT_HISTORY_MAX_CHARS)

    def request_terminal_redraw(self, device_id: str, run_id: str, owner: str) -> bool:
        """Request a full-screen program redraw through its writer thread."""
        with self._device_lock(device_id):
            sess = self.sessions.get(device_id)
            if (sess is None or sess.owner != owner or sess.run_id != run_id
                    or sess.writer is None or sess.rows is None):
                return False
            sess.writer.resize(sess.cols, sess.rows - 1)
            sess.writer.resize(sess.cols, sess.rows)
            return True

    # ---- idle timeout --------------------------------------------------------

    def _sweep_expired_host_key_prompts(self, now: float) -> None:
        """Drop any host-key prompt older than HOST_KEY_PENDING_SECONDS, and
        release the reservation it was holding open."""
        for device_id in list(self._pending):
            with self._device_lock(device_id):
                pending = self._pending.get(device_id)
                if pending is None or (now - pending.created_at) < HOST_KEY_PENDING_SECONDS:
                    continue
                self._pending.pop(device_id, None)
                sess = self.sessions.get(device_id)
                expired_sess = sess if (
                    sess is not None
                    and sess.generation == pending.generation
                    and sess.state == "host_key_pending"
                ) else None
                pop_owner = pending.owner

            if expired_sess is not None:
                self._log(device_id, "Host-key prompt expired. Password cleared from memory.",
                           "warn", pop_owner)
                self._close(device_id, expired_sess)

    def tick_idle(self):
        """Called from the asyncio idle loop every 2 s.  Warns and disconnects idle owners."""
        now = time.monotonic()
        self._sweep_expired_host_key_prompts(now)
        with self._owner_activity_lock:
            owners = list(self._owner_activity)

        for owner in owners:
            owned = [(did, s) for did, s in list(self.sessions.items()) if s.owner == owner]
            if not owned:
                self._owner_pop(owner)
                continue

            busy = [s for _, s in owned if s.busy]
            if busy and any(now - s.last_io < config.TERM_SILENCE_SECONDS for s in busy):
                with self._owner_activity_lock:
                    info = self._owner_activity.get(owner)
                    if info:
                        info["last_active"] = now
                        info["warned"] = False
                        info["silent"] = False
                continue
            if busy:
                # Silent past the limit: idle from the moment of crossing, so
                # the warning goes out now and the timeout follows as usual.
                with self._owner_activity_lock:
                    info = self._owner_activity.get(owner)
                    if info and not info.get("silent"):
                        info["silent"] = True
                        info["last_active"] = now - IDLE_WARN_SECONDS
                        info["warned"] = False

            with self._owner_activity_lock:
                info = self._owner_activity.get(owner)
            if info is None:
                continue
            idle = now - info["last_active"]

            if idle >= IDLE_TIMEOUT_SECONDS:
                # Disconnect all sessions for this owner. Each close is
                # identity-checked against the session snapshotted above, so
                # a session that was replaced between the snapshot and now
                # is left alone.
                for did, s in owned:
                    self._log(did, "Disconnected due to idle timeout.", "warn", owner)
                    self._close(did, s)
                self._push({"type": "ssh_idle_timeout", "_owner": owner})
                self._owner_pop(owner)
            elif idle >= IDLE_WARN_SECONDS and not info["warned"]:
                remaining = int(IDLE_TIMEOUT_SECONDS - idle)
                self._push({"type": "ssh_idle_warning", "seconds": remaining, "_owner": owner})
                with self._owner_activity_lock:
                    info2 = self._owner_activity.get(owner)
                    if info2:
                        info2["warned"] = True

    def stay_connected(self, owner: str) -> dict:
        """Reset idle clock for the signed-in session that clicked Stay connected."""
        self._owner_reset(owner)
        now = time.monotonic()
        for sess in self.sessions.values():
            if sess.owner == owner and sess.busy:
                sess.last_io = now
        return {"ok": True}

    def release_owner(self, owner: str):
        """Close all sessions and host-key prompts owned by a signed-in session."""
        for did, s in list(self.sessions.items()):
            if s.owner == owner:
                self._close(did, s)
        for did, pending in list(self._pending.items()):
            if pending.owner == owner:
                self._clear_pending(did)
        self._owner_pop(owner)

    def locked_device_ids(self) -> list:
        """Return list of device IDs that currently have a live, connected SSH
        session (excludes reservations still "connecting" or awaiting host-key
        approval)."""
        return [did for did, s in self.sessions.items() if s.state == "connected"]

    def owner_ssh_snapshot(self, owner: str) -> tuple[list[str], list[str]]:
        """Return this owner's connected and command-running device IDs.

        Each session is read under its device lock so a reconnecting page sees
        a coherent state even while command workers update their session.
        """
        connected_ids = []
        running_ids = []
        for device_id in list(self.sessions):
            with self._device_lock(device_id):
                session = self.sessions.get(device_id)
                if session is None or session.owner != owner or session.state != "connected":
                    continue
                connected_ids.append(device_id)
                if session.busy:
                    running_ids.append(device_id)
        return connected_ids, running_ids
