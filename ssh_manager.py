"""
SSH session management — ported and adapted from Simple SSH Tool.
Passwords are held in memory only; never written anywhere.
"""

import asyncio
import base64
import hashlib
import os
import re
import threading

import paramiko

BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
KNOWN_HOSTS_FILE = os.path.join(BASE_DIR, "known_hosts")

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


class UnknownHostKey(Exception):
    def __init__(self, hostname, key):
        super().__init__("unknown host key")
        self.hostname = hostname
        self.key      = key


class _TofuPolicy(paramiko.MissingHostKeyPolicy):
    """Trust-on-first-use: surface the offered key instead of auto-accepting."""
    def missing_host_key(self, client, hostname, key):
        raise UnknownHostKey(hostname, key)


class _Session:
    def __init__(self, device: dict, password: str):
        self.device    = device
        self.password  = password   # memory-only
        self.client    = None
        self.busy      = False
        self.channel   = None
        self.cancelled = False

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
    def __init__(self, broadcast_fn):
        """broadcast_fn: async coroutine function that pushes a dict to all WS clients."""
        self._broadcast = broadcast_fn
        self._loop      = None          # set via set_loop() after uvicorn starts
        self.sessions: dict[str, _Session] = {}
        self._pending: dict[str, tuple]    = {}  # device_id -> (host, key)

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    # ---- internal push helpers (safe to call from any thread) -------------

    def _push(self, msg: dict):
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._broadcast(msg), self._loop)

    def _log(self, device_id: str, text: str, level: str = "out"):
        self._push({"type": "ssh_log", "device_id": device_id, "text": text, "level": level})

    def _status(self, device_id: str, state: str):
        self._push({"type": "ssh_status", "device_id": device_id, "state": state})

    # ---- host-key helpers -------------------------------------------------

    def get_host_key(self, host: str) -> dict:
        sub = _load_known_hosts().lookup(host) if host else None
        if not sub:
            return {"known": False, "host": host}
        return {"known": True, "host": host,
                "entries": [{"key_type": kt, "fingerprint": _fp(k)} for kt, k in sub.items()]}

    def trust_host_key(self, device_id: str) -> dict:
        pending = self._pending.pop(device_id, None)
        if not pending:
            return {"ok": False, "error": "No host key is waiting to be trusted."}
        host, key = pending
        try:
            hk = _load_known_hosts()
            if hk.lookup(host):
                del hk[host]
            hk.add(host, key.get_name(), key)
            hk.save(KNOWN_HOSTS_FILE)
            return {"ok": True, "host": host, "fingerprint": _fp(key)}
        except Exception as e:
            return {"ok": False, "error": f"Could not save host key: {e}"}

    def forget_host_key(self, host: str) -> dict:
        try:
            hk = _load_known_hosts()
            if hk.lookup(host):
                del hk[host]
                hk.save(KNOWN_HOSTS_FILE)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- connection -------------------------------------------------------

    def connect(self, device_id: str, password: str, device: dict) -> dict:
        """Synchronous — run via run_in_executor from the async route handler."""
        if device_id in self.sessions:
            self._close(device_id)

        sess = _Session(device, password)
        try:
            sess.connect()
        except UnknownHostKey as e:
            self._pending[device_id] = (e.hostname, e.key)
            return {
                "ok": False, "host_key_unknown": True,
                "host": device["host"], "key_type": e.key.get_name(),
                "fingerprint": _fp(e.key),
            }
        except paramiko.BadHostKeyException as e:
            self._pending[device_id] = (device["host"], e.key)
            return {
                "ok": False, "host_key_changed": True,
                "host": device["host"], "key_type": e.key.get_name(),
                "new_fingerprint": _fp(e.key),
                "old_fingerprint": _fp(e.expected_key),
            }
        except paramiko.AuthenticationException:
            return {"ok": False, "error": "Authentication failed. Check username and password."}
        except Exception as e:
            return {"ok": False, "error": f"Could not connect: {e}"}

        self._pending.pop(device_id, None)
        self.sessions[device_id] = sess
        self._log(device_id, f"Connected to {device['host']} as {device['username']}.", "ok")
        self._status(device_id, "connected")
        return {"ok": True}

    def _close(self, device_id: str):
        sess = self.sessions.pop(device_id, None)
        if sess:
            sess.close()
            self._log(device_id, "Disconnected. Password cleared from memory.", "muted")
        self._status(device_id, "idle")

    def disconnect(self, device_id: str) -> dict:
        self._close(device_id)
        return {"ok": True}

    def disconnect_all(self) -> dict:
        for did in list(self.sessions):
            self._close(did)
        return {"ok": True}

    def cancel(self, device_id: str) -> dict:
        sess = self.sessions.get(device_id)
        if not sess or not sess.channel:
            return {"ok": False}
        sess.cancelled = True
        try:
            sess.channel.close()
        except Exception:
            pass
        return {"ok": True}

    # ---- command execution ------------------------------------------------

    def run_command(self, device_id: str, raw_cmd: str, use_sudo: bool, label: str = None) -> dict:
        sess = self.sessions.get(device_id)
        if not sess:
            return {"ok": False, "error": "Not connected."}
        raw_cmd = (raw_cmd or "").strip()
        if not raw_cmd:
            return {"ok": False, "error": "Empty command."}
        if sess.busy:
            return {"ok": False, "error": "A command is already running."}
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

        threading.Thread(target=self._exec, args=(device_id, cmd, label, feed), daemon=True).start()
        return {"ok": True}

    def _exec(self, device_id: str, cmd: str, label: str, feed_sudo: bool):
        sess = self.sessions.get(device_id)
        if not sess or not sess.client:
            self._log(device_id, "Not connected.", "err")
            return

        needs_sudo = feed_sudo and "sudo" in cmd
        password   = sess.password if needs_sudo else None

        self._status(device_id, "running")
        self._log(device_id, f"$ {label}", "cmd")
        sess.busy      = True
        sess.cancelled = False

        try:
            stdin, stdout, stderr = sess.client.exec_command(cmd, get_pty=needs_sudo)
            sess.channel = stdout.channel

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

            for raw in iter(stdout.readline, ""):
                if raw == "":
                    break
                line = _clean(raw.rstrip("\n"))
                if not line.strip() or not _safe(line):
                    continue
                self._log(device_id, line, "out")

            err  = stderr.read().decode("utf-8", "replace").strip()
            code = stdout.channel.recv_exit_status()

            for ln in (err.splitlines() if err else []):
                ln = _clean(ln)
                if ln.strip() and _safe(ln):
                    self._log(device_id, ln, "err")

            if sess.cancelled:
                self._log(device_id, f"■ {label} cancelled.", "warn")
            elif code == 0:
                self._log(device_id, f"✓ {label} finished.", "ok")
            else:
                self._log(device_id, f"✗ {label} exited with code {code}.", "err")

        except Exception as e:
            if sess and sess.cancelled:
                self._log(device_id, f"■ {label} cancelled.", "warn")
            else:
                self._log(device_id, f"Error: {e}", "err")
        finally:
            if sess:
                sess.busy    = False
                sess.channel = None
            self._status(device_id, "connected")
