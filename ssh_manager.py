"""
SSH session management — ported and adapted from Simple SSH Tool.
Passwords are held in memory only; never written anywhere.
"""

import asyncio
import base64
import codecs
import hashlib
import hmac
import os
import re
import secrets
import threading
import time

import paramiko

IDLE_WARN_SECONDS    = 270   # send warning after 4.5 minutes idle
IDLE_TIMEOUT_SECONDS = 300   # disconnect after 5 minutes idle

HOST_KEY_PENDING_SECONDS = 300   # a host-key prompt expires 5 minutes after it is raised

_EXPIRED_ERROR = "This host-key prompt expired or was already used. Reconnect to try again."

DATA_DIR         = "/var/lib/simple-network-dashboard"
KNOWN_HOSTS_FILE = os.path.join(DATA_DIR, "known_hosts")

# Strip ANSI escape sequences and dpkg progress spam from command output
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[ -/]*[0-~]"
)
_PROGRESS_RE = re.compile(r"^Progress: \[\s*\d+%\]$")
_BAR_RE       = re.compile(r"^\[[#.\s]*\]$")


def _clean(line: str) -> str:
    line = _ANSI_RE.sub("", line)
    if "\r" in line:
        parts = [p for p in line.split("\r") if p.strip()]
        line = parts[-1] if parts else ""
    s = line.strip()
    if _PROGRESS_RE.match(s) or _BAR_RE.match(s):
        return ""
    return line


def _fp(key) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _load_known_hosts() -> paramiko.HostKeys:
    hk = paramiko.HostKeys()
    if os.path.exists(KNOWN_HOSTS_FILE):
        try:
            hk.load(KNOWN_HOSTS_FILE)
        except Exception:
            pass
    return hk


def _save_known_hosts(host_keys: paramiko.HostKeys) -> None:
    host_keys.save(KNOWN_HOSTS_FILE)
    os.chmod(KNOWN_HOSTS_FILE, 0o600)


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
    (browser id) that raised it, and the reserved session's generation the
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
        self.cancelled         = False
        self.cancel_requested  = False
        self.owner             = owner
        self.state             = "connecting"
        self.generation        = 0
        self.last_active       = time.monotonic()

    def connect(self):
        c = paramiko.SSHClient()
        if os.path.exists(KNOWN_HOSTS_FILE):
            try:
                c.load_host_keys(KNOWN_HOSTS_FILE)
            except Exception:
                pass
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

    @staticmethod
    def _sudo_prefix() -> str:
        return "sudo -S -p ''"

    def close(self):
        try:
            if self.client:
                self.client.close()
        finally:
            self.client   = None
            self.channel  = None
            self.password = None   # wipe from memory


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
        self._loop      = None          # set via set_loop() after uvicorn starts
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
        self._loop = loop

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
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._deliver(msg), self._loop)

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
            current = self.sessions.get(msg.get("device_id"))
            if current is None or current.generation != gen:
                return
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

    def _lock(self, device_id: str, locked: bool):
        """Broadcast lock signal to everyone (no _owner key)."""
        self._push({"type": "ssh_lock", "device_id": device_id, "locked": locked})

    # ---- host-key helpers -------------------------------------------------

    def get_host_key(self, host: str) -> dict:
        sub = _load_known_hosts().lookup(host) if host else None
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

        try:
            hk = _load_known_hosts()
            if hk.lookup(host):
                del hk[host]
            hk.add(host, key.get_name(), key)
            _save_known_hosts(hk)
            return {"ok": True, "host": host, "fingerprint": _fp(key)}
        except Exception as e:
            self._debug_write(f"trust_host_key failed: {type(e).__name__}: {e}")
            return {"ok": False, "error": "Could not save the host key."}

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
            return {"ok": False, "error": "Authentication failed. Check username and password."}
        except Exception as e:
            with self._device_lock(device_id):
                if self.sessions.get(device_id) is sess:
                    del self.sessions[device_id]
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
        self._log(device_id, f"Connected to {device['host']} as {device['username']}.", "ok", owner)
        self._status(device_id, "connected", owner)
        self._lock(device_id, True)
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

    def cancel(self, device_id: str, owner: str = None) -> dict:
        with self._device_lock(device_id):
            sess = self.sessions.get(device_id)
            if not sess or not sess.busy:
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

        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass
        return {"ok": True}

    # ---- command execution ------------------------------------------------

    def run_command(self, device_id: str, raw_cmd: str, use_sudo: bool,
                    label: str = None, owner: str = None, cmd_id: str = None) -> dict:
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

        label = label or "Custom command"
        if use_sudo:
            if raw_cmd.startswith("sudo "):
                cmd = raw_cmd.replace("sudo ", f"{_Session._sudo_prefix()} ", 1)
            else:
                inner = raw_cmd.replace("'", "'\\''")
                cmd = f"{_Session._sudo_prefix()} bash -c '{inner}'"
            feed = True
        else:
            cmd  = raw_cmd
            feed = False

        threading.Thread(target=self._exec, args=(sess, cmd, label, feed, cmd_id), daemon=True).start()
        return {"ok": True}

    def _exec(self, sess: "_Session", cmd: str, label: str, feed_sudo: bool, cmd_id: str = None):
        device_id = sess.device_id
        # Captured once: this worker may emit or change state only while it
        # remains current. Every emit below is stamped with this generation
        # too, so a message already queued for delivery is dropped if a
        # disconnect/reconnect replaces the session before it is delivered.
        gen = sess.generation

        if not sess.client:
            if self._is_current(device_id, sess):
                self._log(device_id, "Not connected.", "err", gen=gen)
            self._finish_exec(sess)
            return

        needs_sudo = feed_sudo and "sudo" in cmd
        password   = sess.password if needs_sudo else None

        if self._is_current(device_id, sess):
            self._status(device_id, "running", gen=gen)
            self._log(device_id, f"$ {label}", "cmd", cmd_id=cmd_id, gen=gen)

        if sess.cancel_requested:
            # A cancel arrived before the channel even existed; honor it
            # without ever starting the command.
            sess.cancelled = True
            if self._is_current(device_id, sess):
                self._log(device_id, f"■ {label} cancelled.", "warn", gen=gen)
            self._finish_exec(sess)
            return

        try:
            stdin, stdout, stderr = sess.client.exec_command(cmd, get_pty=needs_sudo)
            chan = stdout.channel
            sess.channel = chan
            if sess.cancel_requested:
                sess.cancelled = True
                try:
                    chan.close()
                except Exception:
                    pass

            if needs_sudo and password:
                try:
                    stdin.write(password + "\n")
                    stdin.flush()
                except Exception:
                    pass

            def _safe(s: str) -> bool:
                s = s.strip()
                if not s:
                    return True
                if password and s == password:
                    return False
                if s.startswith("[sudo] password for"):
                    return False
                return True

            # Read the raw channel ourselves, interleaving stdout and stderr
            # as data arrives, instead of draining stdout to EOF before ever
            # looking at stderr. A pty-backed sudo command merges stderr into
            # the stdout stream, so recv_stderr_ready() simply stays false for
            # those; a plain command keeps the two apart. select() cannot
            # watch a paramiko channel on Windows, so this polls instead.
            out_decoder = codecs.getincrementaldecoder("utf-8")("replace")
            err_decoder = codecs.getincrementaldecoder("utf-8")("replace")
            out_buf = ""
            err_buf = ""

            def _split_and_emit(buf: str, level: str) -> str:
                """Emit every complete line in `buf`, returning the trailing
                partial line (kept for the next chunk). Stops emitting, but
                still returns the remainder, once this worker is no longer
                current."""
                if "\n" not in buf:
                    return buf
                *complete, remainder = buf.split("\n")
                for raw_line in complete:
                    if not self._is_current(device_id, sess):
                        return remainder
                    cleaned = _clean(raw_line)
                    if cleaned.strip() and _safe(cleaned):
                        self._log(device_id, cleaned, level, gen=gen)
                return remainder

            superseded = False
            while True:
                if not self._is_current(device_id, sess):
                    superseded = True
                    break

                got_data = False
                if chan.recv_ready():
                    raw = chan.recv(32768)
                    if raw:
                        got_data = True
                        out_buf += out_decoder.decode(raw, False)
                        out_buf = _split_and_emit(out_buf, "out")
                if chan.recv_stderr_ready():
                    raw = chan.recv_stderr(32768)
                    if raw:
                        got_data = True
                        err_buf += err_decoder.decode(raw, False)
                        err_buf = _split_and_emit(err_buf, "err")

                if not got_data:
                    if (chan.exit_status_ready()
                            and not chan.recv_ready()
                            and not chan.recv_stderr_ready()):
                        break
                    time.sleep(0.02)

            if not superseded:
                # Final non-blocking drain: bytes that arrived right at exit.
                while chan.recv_ready():
                    raw = chan.recv(32768)
                    if not raw:
                        break
                    out_buf += out_decoder.decode(raw, False)
                    out_buf = _split_and_emit(out_buf, "out")
                while chan.recv_stderr_ready():
                    raw = chan.recv_stderr(32768)
                    if not raw:
                        break
                    err_buf += err_decoder.decode(raw, False)
                    err_buf = _split_and_emit(err_buf, "err")

            if not self._is_current(device_id, sess):
                return

            # Flush each decoder and emit any trailing line with no newline.
            out_buf += out_decoder.decode(b"", True)
            err_buf += err_decoder.decode(b"", True)
            if out_buf:
                cleaned = _clean(out_buf)
                if cleaned.strip() and _safe(cleaned):
                    self._log(device_id, cleaned, "out", gen=gen)
            if err_buf:
                cleaned = _clean(err_buf)
                if cleaned.strip() and _safe(cleaned):
                    self._log(device_id, cleaned, "err", gen=gen)

            code = chan.recv_exit_status()

            if sess.cancelled:
                self._log(device_id, f"■ {label} cancelled.", "warn", gen=gen)
            elif code == 0:
                self._log(device_id, f"✓ {label} finished.", "ok", gen=gen)
            else:
                self._log(device_id, f"✗ {label} exited with code {code}.", "err", gen=gen)

        except Exception as e:
            if not self._is_current(device_id, sess):
                pass
            elif sess.cancelled:
                self._log(device_id, f"■ {label} cancelled.", "warn", gen=gen)
            else:
                self._log(device_id, f"Error: {e}", "err", gen=gen)
        finally:
            self._finish_exec(sess)

    def _finish_exec(self, sess: "_Session"):
        """Identity-checked cleanup: clear busy/channel, reset the idle
        clock, and emit the trailing "connected" status only if this session
        is still the current one for its device."""
        device_id = sess.device_id
        with self._device_lock(device_id):
            if self.sessions.get(device_id) is sess:
                sess.busy    = False
                sess.channel = None
                owner = sess.owner
                still_current = True
            else:
                owner = None
                still_current = False

        if still_current:
            if owner:
                self._owner_reset(owner)
            # Stamped with this session's generation: the identity check
            # above only guards against a replace that already happened; a
            # replace landing between here and delivery is caught at
            # delivery time by the generation stamp.
            self._status(device_id, "connected", gen=sess.generation)

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

            # If any session is busy, treat the owner as active
            if any(s.busy for _, s in owned):
                with self._owner_activity_lock:
                    info = self._owner_activity.get(owner)
                    if info:
                        info["last_active"] = now
                        info["warned"] = False
                continue

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
        """Reset idle clock for a browser that clicked 'Stay connected'."""
        self._owner_reset(owner)
        return {"ok": True}

    def release_owner(self, owner: str):
        """Close all sessions owned by a browser (used when its last WS tab closes)."""
        for did, s in list(self.sessions.items()):
            if s.owner == owner:
                self._close(did, s)
        self._owner_pop(owner)

    def locked_device_ids(self) -> list:
        """Return list of device IDs that currently have a live, connected SSH
        session (excludes reservations still "connecting" or awaiting host-key
        approval)."""
        return [did for did, s in self.sessions.items() if s.state == "connected"]

    def owner_connected_ids(self, owner: str) -> list:
        """Return the device IDs this owner currently has a connected (not
        merely reserved) SSH session on."""
        return [did for did, s in self.sessions.items() if s.owner == owner and s.state == "connected"]
