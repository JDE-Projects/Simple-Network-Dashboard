"""
Simple Network Dashboard — FastAPI backend.

Serves the static UI, exposes REST endpoints for device/SSH management,
and pushes real-time metrics + SSH events over a WebSocket.
"""

import asyncio
import copy
import errno
import hmac
import json
import os
import secrets
import socket
import ssl
import stat
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from ipaddress import ip_address
from typing import Annotated, Optional

from argon2.exceptions import Argon2Error
from fastapi import FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from metrics_poller import fetch_metrics
from ssh_manager import SSHManager
from auth import load_auth_state, verify_password
from session_manager import LoginThrottle, REMEMBERED_SECONDS, SessionStorageError, SessionStore

METRICS_INTERVAL = 2  # seconds between polls for the selected device
WS_RELEASE_GRACE_SECONDS = 15  # grace period before a disconnected browser's SSH sessions are released, lets a page refresh reconnect without losing sessions
MAX_REQUEST_BODY_BYTES = 1_048_576  # 1 MB cap on incoming HTTP request bodies
MAX_DEVICES = 250  # cap on total saved devices
MAX_COMMANDS_PER_DEVICE = 250  # cap on saved commands per device, per request
MAX_NAME_LEN = 80  # device name / saved command name character cap
MAX_HOST_LEN = 253  # hostname character cap (RFC 1035 full name length)
MAX_USERNAME_LEN = 64  # SSH username character cap
MAX_COMMAND_LEN = 4096  # saved command text / run command character cap
MAX_CONFIRM_LEN = 500  # saved command confirmation prompt character cap
MIN_METRICS_PORT = 1
MAX_METRICS_PORT = 65535

APP_NAME    = "Simple Network Dashboard"
APP_VERSION = "1.5.1"
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = "/var/lib/simple-network-dashboard"
LOG_DIR = "/var/log/simple-network-dashboard"
DEVICES_FILE = os.path.join(DATA_DIR, "devices.json")
DASHBOARD_ROOT_CERT = os.path.join(DATA_DIR, "caddy-root-ca.crt")
AUTH_FILE = os.path.join(DATA_DIR, "auth.json")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
SESSION_COOKIE_NAME = "__Host-snd-session"
CSRF_COOKIE_NAME = "__Host-snd-csrf"
CSRF_HEADER_NAME = "x-csrf-token"
PUBLIC_ORIGIN_ENV = "SND_PUBLIC_ORIGIN"
WS_POLICY_VIOLATION_CODE = 1008
_PUBLIC_PATHS = {
    "/login",
    "/api/auth/login",
    "/api/auth/session",
    "/certificate-setup",
    "/certificate-setup/caddy-root-ca.crt",
}
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'{wss_source}; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

class _WSManager:
    def __init__(self):
        self._connections: list[WebSocket] = []
        self._owners: dict[WebSocket, str] = {}  # ws -> owner (browser id)

    async def connect(self, ws: WebSocket, owner_id: str = None):
        await ws.accept()
        self._connections.append(ws)
        if owner_id:
            self._owners[ws] = owner_id

    def drop(self, ws: WebSocket):
        self._connections = [c for c in self._connections if c is not ws]
        self._owners.pop(ws, None)

    def owner_of(self, ws: WebSocket) -> str:
        return self._owners.get(ws)

    def owner_count(self, owner_id: str) -> int:
        return sum(1 for o in self._owners.values() if o == owner_id)

    async def broadcast(self, msg: dict):
        if not self._connections:
            return
        data = json.dumps(msg)
        dead = []
        for ws in self._connections:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.drop(ws)

    async def send_to_owner(self, owner_id: str, msg: dict):
        if not self._connections or not owner_id:
            return
        data = json.dumps(msg)
        dead = []
        for ws in self._connections:
            if self._owners.get(ws) != owner_id:
                continue
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.drop(ws)


ws_mgr = _WSManager()

# Latest metrics per device (includes _raw fields for delta calculation)
_metrics_cache: dict[str, dict] = {}

# Device ID currently selected in the browser (None = no device selected / no clients)
_selected_device_id: Optional[str] = None

# In-memory device list — seeded from disk at startup, kept in sync by _save()
_devices_cache: list = []

# True if configuration changes cannot be persisted (checked once at startup)
_storage_warning = False

# True only when startup recovered the cached configuration from devices.json.bak.
# It deliberately remains latched until the service is restarted.
_recovery_mode = False

# True only for a startup that repaired the primary configuration from its backup.
_recovered_notice = False

# Exact already-validated backup payload retained only while fallback recovery is read-only.
_recovery_backup_contents: str | None = None
_recovery_retry_lock = asyncio.Lock()

# Pending SSH-release tasks per owner, scheduled when their last socket drops
_pending_releases: dict[str, asyncio.Task] = {}
_session_store = SessionStore(SESSIONS_FILE)
_login_throttle = LoginThrottle()
_login_lock = asyncio.Lock()


def _cancel_pending_release(owner: str):
    task = _pending_releases.pop(owner, None)
    if task:
        task.cancel()


async def _release_after_grace(owner: str):
    await asyncio.sleep(WS_RELEASE_GRACE_SECONDS)
    _pending_releases.pop(owner, None)
    # Re-check in case a reconnect arrived but cancellation hasn't landed yet
    if ws_mgr.owner_count(owner) == 0:
        ssh_mgr.release_owner(owner)

# Debug log file handle — None when disabled
_debug_file = None


def _debug_write(text: str):
    if _debug_file is not None:
        stamp = datetime.now().strftime("%H:%M:%S")
        _debug_file.write(f"[{stamp}] {text}\n")


async def _broadcast(msg: dict):
    """Broadcast wrapper that also writes SSH events to the debug log.
    If msg contains a private '_owner' key, route to that owner only;
    otherwise broadcast to everyone.  The key is popped before sending."""
    if _debug_file is not None:
        t   = msg.get("type", "")
        did = msg.get("device_id", "")
        if t == "ssh_log":
            _debug_write(f"SSH [{did}] {msg.get('level', 'out').upper()}: {msg.get('text', '')}")
        elif t == "ssh_status":
            _debug_write(f"SSH [{did}] → {msg.get('state', '')}")
    owner = msg.pop("_owner", None)
    if owner:
        await ws_mgr.send_to_owner(owner, msg)
    else:
        await ws_mgr.broadcast(msg)


ssh_mgr = SSHManager(_broadcast, _debug_write)


# ---------------------------------------------------------------------------
# Device persistence
# ---------------------------------------------------------------------------

def _parse_devices(contents: str) -> list:
    """Parse and normalize a devices.json payload."""
    data = json.loads(contents)
    devices = data.get("devices", data) if isinstance(data, dict) else data
    return [_norm(d) for d in devices if isinstance(d, dict)]


def _load_startup_devices() -> tuple[list, bool, bool]:
    """Load the primary once, repairing it from a valid backup when possible.

    Returns devices, whether a repair succeeded, and whether failed repair left
    the process in restart-latched read-only mode.
    """
    global _recovery_backup_contents
    _recovery_backup_contents = None
    try:
        with open(DEVICES_FILE, "r", encoding="utf-8") as f:
            return _parse_devices(f.read()), False, False
    except Exception as primary_error:
        try:
            with open(f"{DEVICES_FILE}.bak", "r", encoding="utf-8") as f:
                backup_contents = f.read()
            devices = _parse_devices(backup_contents)
        except Exception as backup_error:
            if isinstance(primary_error, FileNotFoundError) and isinstance(
                backup_error, FileNotFoundError
            ):
                return [], False, False
            print(
                "WARNING: Configuration recovery mode is active. "
                "Neither configuration copy could be loaded; "
                "configuration changes are disabled to protect the existing files.",
                flush=True,
            )
            return [], False, True
        try:
            _replace_primary_from_backup(backup_contents)
            with open(DEVICES_FILE, "r", encoding="utf-8") as f:
                _parse_devices(f.read())
        except Exception as repair_error:
            _recovery_backup_contents = backup_contents
            print(
                "WARNING: Configuration recovery mode is active. "
                "Automatic restoration from the validated backup did not complete "
                f"({type(repair_error).__name__}); "
                "configuration changes are disabled until recovery succeeds.",
                flush=True,
            )
            return devices, False, True
        print(
            "NOTICE: Configuration was restored automatically from its validated backup because "
            f"{_configuration_failure_reason(primary_error)}.",
            flush=True,
        )
        return devices, True, False


def _configuration_failure_reason(error: Exception) -> str:
    """Describe a primary-load failure without including file contents or raw errors."""
    if isinstance(error, FileNotFoundError):
        return "the primary configuration is missing"
    if isinstance(error, json.JSONDecodeError):
        return "the primary configuration contains invalid JSON"
    if isinstance(error, UnicodeError):
        return "the primary configuration is not valid UTF-8"
    if isinstance(error, OSError):
        return f"the primary configuration could not be read ({type(error).__name__})"
    return f"the primary configuration could not be normalized ({type(error).__name__})"


# Shared error message for endpoints that fail to persist a device change
_SAVE_ERROR = "Server could not write devices.json (check file ownership/permissions on the server)."
_RECOVERY_READ_ONLY_ERROR = (
    "Configuration changes are unavailable while the dashboard is in recovery mode."
)
_DEVICE_LIMIT_ERROR = "The device limit of 250 has been reached."
_UNKNOWN_DEVICE_ERROR = "That device does not exist."
_REQUEST_BODY_TOO_LARGE_ERROR = "Request body is too large."


def _open_private_file(path: str, *, buffering: int = -1):
    fd = None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        file = os.fdopen(fd, "w", encoding="utf-8", buffering=buffering)
        fd = None
        return file
    except Exception:
        if fd is not None:
            os.close(fd)
        raise


def _open_private_temp_file() -> tuple[str, object]:
    """Create a private, same-directory file for an atomic device save."""
    fd = None
    path = None
    try:
        fd, path = tempfile.mkstemp(prefix=".devices-", suffix=".tmp", dir=DATA_DIR)
        os.fchmod(fd, 0o600)
        file = os.fdopen(fd, "w", encoding="utf-8")
        fd = None
        return path, file
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if path is not None:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise


def _fsync_data_directory() -> None:
    """Persist the replacement entry after the temporary file is replaced."""
    fd = None
    try:
        fd = os.open(DATA_DIR, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(fd)
    finally:
        if fd is not None:
            os.close(fd)


def _write_private_payload(path: str, contents: str, on_replaced=None) -> None:
    """Durably replace one private configuration file with an exact payload."""
    temp_path = None
    try:
        temp_path, f = _open_private_temp_file()
        with f:
            f.write(contents)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
        temp_path = None
        if on_replaced is not None:
            on_replaced()
        _fsync_data_directory()
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _replace_primary_from_backup(contents: str) -> None:
    """Atomically restore the exact validated backup payload to the primary."""
    _write_private_payload(DEVICES_FILE, contents)


def _restore_and_verify_primary(contents: str) -> None:
    """Restore an already-validated payload and prove the replacement can load."""
    _replace_primary_from_backup(contents)
    with open(DEVICES_FILE, "r", encoding="utf-8") as f:
        _parse_devices(f.read())


def _save(devices: list) -> bool:
    global _devices_cache
    if _recovery_mode:
        msg = "SAVE REJECTED: configuration recovery mode is read-only until recovery succeeds."
        _debug_write(msg)
        print(msg, flush=True)
        return False
    replaced = False

    def primary_replaced() -> None:
        global _devices_cache
        nonlocal replaced
        replaced = True
        _devices_cache = list(devices)

    try:
        contents = json.dumps({"_app": APP_NAME, "devices": devices}, indent=2)
        _parse_devices(contents)
        _write_private_payload(DEVICES_FILE, contents, primary_replaced)
        _write_private_payload(f"{DEVICES_FILE}.bak", contents)
        return True
    except Exception:
        if replaced:
            msg = "SAVE UNCONFIRMED: primary configuration was replaced but the durable mirrored save did not complete."
        else:
            msg = "SAVE FAILED: could not persist device configuration."
        _debug_write(msg)
        print(msg, flush=True)  # always visible in the systemd journal, even with debug logging off
        return False


def _norm(d: dict) -> dict:
    cmds = d.get("commands", [])
    if not isinstance(cmds, list):
        cmds = []
    clean = []
    for c in cmds:
        if not isinstance(c, dict):
            continue
        cmd = (c.get("command") or "").strip()
        if not cmd:
            continue
        clean.append({
            "name":    (c.get("name") or cmd[:24]).strip(),
            "command": cmd,
            "sudo":    bool(c.get("sudo", False)),
            "confirm": (c.get("confirm") or "").strip(),
        })
    d["commands"]     = clean
    d["metrics_port"] = int(d.get("metrics_port") or 9100)
    return d


# ---------------------------------------------------------------------------
# Metrics polling loop (background asyncio task)
# ---------------------------------------------------------------------------

async def _metrics_loop():
    while True:
        if ws_mgr._connections and _selected_device_id:
            device = next((d for d in _devices_cache if d.get("id") == _selected_device_id), None)
            if device:
                did  = device.get("id")
                host = device.get("host")
                port = device.get("metrics_port", 9100)
                prev    = _metrics_cache.get(did)
                metrics = await fetch_metrics(host, port, prev)
                _metrics_cache[did] = metrics
                if metrics.get("error"):
                    _debug_write(f"METRICS [{did}] {host}:{port} → {metrics['error']}")
                public = {k: v for k, v in metrics.items() if not k.startswith("_")}
                await ws_mgr.broadcast({"type": "metrics", "device_id": did, "data": public})
        await asyncio.sleep(METRICS_INTERVAL)


async def _idle_loop():
    while True:
        ssh_mgr.tick_idle()
        await asyncio.sleep(2)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _devices_cache, _storage_warning, _recovery_mode, _recovered_notice
    _devices_cache, _recovered_notice, _recovery_mode = _load_startup_devices()
    _storage_warning = _recovery_mode
    if os.path.exists(DEVICES_FILE) and not os.access(DEVICES_FILE, os.W_OK):
        _storage_warning = True
        print(f"WARNING: {DEVICES_FILE} is not writable. Device and command changes will NOT be saved.", flush=True)
    ssh_mgr.set_loop(asyncio.get_running_loop())
    metrics_task = asyncio.create_task(_metrics_loop())
    idle_task    = asyncio.create_task(_idle_loop())
    yield
    metrics_task.cancel()
    idle_task.cancel()
    ssh_mgr.disconnect_all()
    if _debug_file is not None:
        _debug_write("=== Debug log closed (server shutdown) ===")
        _debug_file.close()


app = FastAPI(title=APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


def _is_public_path(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith("/static/")


def _set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )


def _expire_csrf_cookie(response: Response) -> None:
    response.delete_cookie(CSRF_COOKIE_NAME, path="/", secure=True, httponly=False, samesite="strict")


def _csrf_is_valid(request: Request) -> bool:
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    header_token = request.headers.get(CSRF_HEADER_NAME)
    return bool(cookie_token and header_token and hmac.compare_digest(cookie_token, header_token))


def _same_host_wss_source(request: Request) -> str:
    """Build a CSP source for the browser's exact HTTPS host and port."""
    hostname = request.url.hostname
    if not hostname:
        return ""
    try:
        address = ip_address(hostname)
        source_host = f"[{address}]" if address.version == 6 else str(address)
    except ValueError:
        try:
            hostname.encode("ascii")
        except UnicodeEncodeError:
            return ""
        labels = hostname.rstrip(".").split(".")
        if (
            len(hostname) > 253
            or any(not label or len(label) > 63 for label in labels)
            or any(label[0] == "-" or label[-1] == "-" for label in labels)
            or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-." for character in hostname)
        ):
            return ""
        source_host = hostname.rstrip(".")
    try:
        port = request.url.port
    except ValueError:
        return ""
    return f" wss://{source_host}{f':{port}' if port is not None else ''}"


def _add_security_headers(response: Response, request: Request) -> Response:
    path = request.url.path
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=(), payment=(), usb=()"
    response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY.format(
        wss_source=_same_host_wss_source(request)
    )
    if not path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(Exception)
async def internal_server_error(request: Request, _error: Exception):
    """Keep unexpected HTTP failures private and apply browser protections."""
    response = JSONResponse({"detail": "Internal server error."}, status_code=500)
    return _add_security_headers(response, request)


@app.exception_handler(RequestValidationError)
async def request_validation_error(request: Request, _error: RequestValidationError):
    """Reshape FastAPI's default validation-error body into the app's own shape."""
    response = JSONResponse({"ok": False, "error": "The request was not valid."}, status_code=422)
    return _add_security_headers(response, request)


@app.middleware("http")
async def protect_http_requests(request: Request, call_next):
    """Enforce browser-session and double-submit CSRF rules for HTTP only."""
    path = request.url.path
    is_public = _is_public_path(path)

    if path == "/api/auth/login":
        if request.method in _UNSAFE_METHODS and not _csrf_is_valid(request):
            return _add_security_headers(JSONResponse({"detail": "CSRF validation failed."}, status_code=403), request)
    elif not is_public:
        if not await _current_session_is_valid(request):
            if path.startswith("/api/"):
                return _add_security_headers(JSONResponse({"detail": "Authentication required."}, status_code=401), request)
            response = Response(status_code=307, headers={"Location": "/login"})
            return _add_security_headers(response, request)
        if request.method in _UNSAFE_METHODS and not _csrf_is_valid(request):
            return _add_security_headers(JSONResponse({"detail": "CSRF validation failed."}, status_code=403), request)

    response = await call_next(request)
    needs_csrf_cookie = (
        request.method == "GET"
        and not request.cookies.get(CSRF_COOKIE_NAME)
        and (path == "/login" or not is_public)
    )
    if needs_csrf_cookie:
        _set_csrf_cookie(response, secrets.token_urlsafe(32))
    return _add_security_headers(response, request)


@app.middleware("http")
async def limit_request_body_size(request: Request, call_next):
    """Reject oversized request bodies before they reach any handler.

    Checks the declared Content-Length header first as a fast rejection, then
    checks the bytes actually read while draining the body, so a missing or
    lying Content-Length header cannot slip an oversized body through.

    Downstream handlers read the body via ``request.body()``/``request.json()``,
    which Starlette serves from ``request._body`` once populated. Filling that
    cache here (instead of handing call_next a fresh Request) lets us abort as
    soon as the running total crosses the limit, rather than buffering an
    unbounded body first.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > MAX_REQUEST_BODY_BYTES:
            return _add_security_headers(
                JSONResponse({"ok": False, "error": _REQUEST_BODY_TOO_LARGE_ERROR}, status_code=413),
                request,
            )

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_REQUEST_BODY_BYTES:
            return _add_security_headers(
                JSONResponse({"ok": False, "error": _REQUEST_BODY_TOO_LARGE_ERROR}, status_code=413),
                request,
            )

    request._body = bytes(body)  # noqa: SLF001 - populates Starlette's own body cache
    return await call_next(request)


@app.get("/")
async def root():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@app.get("/login")
async def login_page():
    return FileResponse(os.path.join(BASE_DIR, "static", "login.html"))


def _effective_client_address(request: Request) -> str:
    """Use Caddy's client address only for loopback backend requests."""
    peer = request.client.host if request.client else ""
    try:
        loopback_peer = ip_address(peer).is_loopback
    except ValueError:
        loopback_peer = False
    forwarded = request.headers.get("x-forwarded-for", "")
    if loopback_peer and forwarded:
        candidate = forwarded.split(",", 1)[0].strip()
        try:
            return str(ip_address(candidate))
        except ValueError:
            pass
    return peer or "unknown"


def _set_session_cookie(response: Response, token: str, remembered: bool) -> None:
    options: dict[str, object] = {
        "key": SESSION_COOKIE_NAME,
        "value": token,
        "path": "/",
        "secure": True,
        "httponly": True,
        "samesite": "strict",
    }
    if remembered:
        options["max_age"] = REMEMBERED_SECONDS
    response.set_cookie(**options)


def _expire_session_cookie(response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict"
    )


async def _session_token_is_valid(token: str | None) -> bool:
    try:
        state = await asyncio.to_thread(load_auth_state, AUTH_FILE)
        return await asyncio.to_thread(_session_store.validate, token, state["session_generation"])
    except (OSError, ValueError, SessionStorageError):
        return False


async def _current_session_is_valid(request: Request) -> bool:
    return await _session_token_is_valid(request.cookies.get(SESSION_COOKIE_NAME))


async def _websocket_request_is_valid(ws: WebSocket) -> bool:
    """Validate the authenticated browser and Caddy-provided public origin."""
    trusted_origin = os.environ.get(PUBLIC_ORIGIN_ENV)
    origins = ws.headers.getlist("origin")
    if not trusted_origin or len(origins) != 1:
        return False
    try:
        origin_matches = hmac.compare_digest(origins[0], trusted_origin)
    except TypeError:
        return False
    if not origin_matches:
        return False
    return await _session_token_is_valid(ws.cookies.get(SESSION_COOKIE_NAME))


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str
    remembered: bool = False


@app.post("/api/auth/login")
async def login(credentials: LoginIn, request: Request):
    address = _effective_client_address(request)
    async with _login_lock:
        retry_after = _login_throttle.retry_after(address)
        if retry_after:
            return JSONResponse(
                {"ok": False, "error": "Too many failed attempts. Try again later.", "retry_after": retry_after},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        try:
            state = await asyncio.to_thread(load_auth_state, AUTH_FILE)
            password_valid = await asyncio.to_thread(verify_password, state, credentials.password)
        except (OSError, ValueError, SessionStorageError, Argon2Error):
            raise HTTPException(status_code=503, detail="Dashboard authentication is unavailable.") from None
        if not password_valid:
            retry_after = _login_throttle.failure(address)
            return JSONResponse(
                {"ok": False, "error": "Password was not accepted.", "retry_after": retry_after},
                status_code=401,
            )
        try:
            token, _expiry = await asyncio.to_thread(
                _session_store.create, state["session_generation"], credentials.remembered
            )
        except (OSError, ValueError, SessionStorageError):
            raise HTTPException(status_code=503, detail="Dashboard authentication is unavailable.") from None
        _login_throttle.success(address)
        response = JSONResponse({"ok": True, "remembered": credentials.remembered})
        _set_session_cookie(response, token, credentials.remembered)
        _set_csrf_cookie(response, secrets.token_urlsafe(32))
        return response


@app.get("/api/auth/session")
async def session_status(request: Request):
    return {"authenticated": await _current_session_is_valid(request)}


@app.post("/api/auth/logout")
async def logout(request: Request):
    try:
        await asyncio.to_thread(_session_store.revoke, request.cookies.get(SESSION_COOKIE_NAME))
    except (OSError, ValueError, SessionStorageError):
        raise HTTPException(status_code=503, detail="Dashboard sign-out is unavailable. Please try again.") from None
    response = JSONResponse({"ok": True})
    _expire_session_cookie(response)
    _expire_csrf_cookie(response)
    return response


@app.get("/certificate-setup")
async def certificate_setup():
    return FileResponse(os.path.join(BASE_DIR, "static", "certificate-setup.html"))


@app.get("/certificate-setup/caddy-root-ca.crt")
async def download_caddy_root_certificate():
    try:
        certificate_mode = os.lstat(DASHBOARD_ROOT_CERT).st_mode
    except OSError:
        raise HTTPException(status_code=404, detail="The exported root certificate is not available yet.") from None

    if not stat.S_ISREG(certificate_mode):
        raise HTTPException(status_code=404, detail="The exported root certificate is not available yet.")

    return FileResponse(
        DASHBOARD_ROOT_CERT,
        media_type="application/x-x509-ca-cert",
        filename="caddy-root-ca.crt",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not await _websocket_request_is_valid(ws):
        await ws.close(code=WS_POLICY_VIOLATION_CODE)
        return
    owner = ws.query_params.get("bid") or ""
    await ws_mgr.connect(ws, owner)
    if owner:
        # Reconnecting within the grace window keeps the owner's SSH sessions
        _cancel_pending_release(owner)
    try:
        # Push current state so a fresh page load (or reconnect) is in sync
        # ssh_connected = devices this owner currently owns
        own_connected = [did for did, s in ssh_mgr.sessions.items() if s.owner == owner]
        await ws.send_text(json.dumps({
            "type": "init", "devices": _devices_cache, "version": APP_VERSION,
            "ssh_connected": own_connected,
            "ssh_locked": ssh_mgr.locked_device_ids(),
            "debug": _debug_file is not None,
            "storage_warning": _storage_warning,
            "recovery_mode": _recovery_mode,
            "recovery_retry_available": _recovery_backup_contents is not None,
            "recovered_notice": _recovered_notice,
        }))
        # Push cached metrics so the stats panel fills immediately
        for did, m in _metrics_cache.items():
            public = {k: v for k, v in m.items() if not k.startswith("_")}
            await ws.send_text(json.dumps({"type": "metrics", "device_id": did, "data": public}))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("type") == "select_device":
                global _selected_device_id
                _selected_device_id = msg.get("id")
            elif msg.get("type") == "stay_connected":
                ssh_mgr.stay_connected(owner)
    except WebSocketDisconnect:
        pass
    finally:
        ws_mgr.drop(ws)
        if owner and ws_mgr.owner_count(owner) == 0:
            # Don't release immediately: a page refresh reconnects moments
            # later and should keep its SSH sessions
            _cancel_pending_release(owner)
            _pending_releases[owner] = asyncio.create_task(_release_after_grace(owner))


# ---------------------------------------------------------------------------
# Device CRUD
# ---------------------------------------------------------------------------

class CommandItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name:    Annotated[str, Field(max_length=MAX_NAME_LEN)] = ""
    command: Annotated[str, Field(max_length=MAX_COMMAND_LEN)]
    sudo:    bool = False
    confirm: Annotated[str, Field(max_length=MAX_CONFIRM_LEN)] = ""


class DeviceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id:           Optional[str] = None
    name:         Annotated[str, Field(max_length=MAX_NAME_LEN)]
    host:         Annotated[str, Field(max_length=MAX_HOST_LEN)]
    username:     Annotated[str, Field(max_length=MAX_USERNAME_LEN)]
    metrics_port: Annotated[int, Field(ge=MIN_METRICS_PORT, le=MAX_METRICS_PORT)] = 9100
    commands:     Optional[Annotated[list[CommandItem], Field(max_length=MAX_COMMANDS_PER_DEVICE)]] = None


@app.get("/api/devices")
async def get_devices():
    return _devices_cache


@app.post("/api/retry-recovery")
async def retry_recovery():
    """Retry a failed startup repair using only the retained validated payload."""
    global _recovery_mode, _storage_warning, _recovery_backup_contents
    async with _recovery_retry_lock:
        if not _recovery_mode:
            return {"ok": True, "recovered": False}
        contents = _recovery_backup_contents
        if contents is None:
            msg = "RECOVERY RETRY FAILED: retained payload unavailable."
            _debug_write(msg)
            print(msg, flush=True)
            return {"ok": False, "error": "No validated backup is available for automatic recovery."}
        try:
            _restore_and_verify_primary(contents)
        except Exception as error:
            msg = f"RECOVERY RETRY FAILED: {type(error).__name__}"
            _debug_write(msg)
            print(msg, flush=True)
            return {"ok": False, "error": "Recovery could not finish. Your saved data remains protected."}
        _recovery_mode = False
        _storage_warning = False
        _recovery_backup_contents = None
        await ws_mgr.broadcast({"type": "recovery_restored"})
        return {"ok": True, "recovered": True}


def _resolve_saved_commands(incoming: Optional[list], existing: Optional[dict]) -> list:
    """Decide the command list to store for a saved device.

    - incoming is None (field omitted): preserve the existing device's
      commands on an update, or start empty for a brand-new device.
    - incoming is [] : the caller explicitly cleared the library.
    - incoming has items: the caller replaced the library with those items.
    """
    if incoming is None:
        return existing.get("commands", []) if existing else []
    return incoming


@app.post("/api/devices")
async def upsert_device(body: DeviceIn):
    if _recovery_mode:
        return {"ok": False, "error": _RECOVERY_READ_ONLY_ERROR}
    d       = body.model_dump()
    devices = copy.deepcopy(_devices_cache)
    if d.get("id"):
        for i, existing in enumerate(devices):
            if existing["id"] == d["id"]:
                d["commands"] = _resolve_saved_commands(d["commands"], existing)
                devices[i] = _norm(d)
                break
        else:
            return {"ok": False, "error": _UNKNOWN_DEVICE_ERROR}
    else:
        if len(devices) >= MAX_DEVICES:
            return {"ok": False, "error": _DEVICE_LIMIT_ERROR}
        d["id"]       = "dev_" + uuid.uuid4().hex[:12]
        d["commands"] = _resolve_saved_commands(d["commands"], None)
        devices.append(_norm(d))
    if not _save(devices):
        return {"ok": False, "error": _SAVE_ERROR}
    await ws_mgr.broadcast({"type": "devices", "devices": devices})
    return {"ok": True, "devices": devices}


@app.delete("/api/devices/{device_id}")
async def delete_device(device_id: str):
    global _selected_device_id
    if _recovery_mode:
        return {"ok": False, "error": _RECOVERY_READ_ONLY_ERROR}
    if _selected_device_id == device_id:
        _selected_device_id = None
    devices = [d for d in _devices_cache if d["id"] != device_id]
    if not _save(devices):
        return {"ok": False, "error": _SAVE_ERROR}
    ssh_mgr.disconnect(device_id)
    await ws_mgr.broadcast({"type": "devices", "devices": devices})
    return {"ok": True, "devices": devices}


class CommandsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commands: Annotated[list[CommandItem], Field(max_length=MAX_COMMANDS_PER_DEVICE)]


@app.put("/api/devices/{device_id}/commands")
async def update_commands(device_id: str, body: CommandsIn):
    if _recovery_mode:
        return {"ok": False, "error": _RECOVERY_READ_ONLY_ERROR}
    devices = copy.deepcopy(_devices_cache)
    for i, d in enumerate(devices):
        if d["id"] == device_id:
            d["commands"] = body.model_dump()["commands"]
            devices[i] = _norm(d)
            break
    if not _save(devices):
        return {"ok": False, "error": _SAVE_ERROR}
    await ws_mgr.broadcast({"type": "devices", "devices": devices})
    return {"ok": True, "devices": devices}


# ---------------------------------------------------------------------------
# SSH endpoints
# ---------------------------------------------------------------------------

class ConnectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str
    password:  str


@app.post("/api/ssh/connect")
async def ssh_connect(body: ConnectIn, x_browser_id: str = Header(None)):
    if not x_browser_id:
        return {"ok": False, "error": "Missing browser id."}
    devices = _devices_cache
    device  = next((d for d in devices if d["id"] == body.device_id), None)
    if not device:
        return {"ok": False, "error": "Device not found."}
    if not body.password:
        return {"ok": False, "error": "Password is required."}
    loop   = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, ssh_mgr.connect, body.device_id, body.password, device, x_browser_id)
    return result


class DeviceIdIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str


@app.post("/api/ssh/disconnect")
async def ssh_disconnect(body: DeviceIdIn, x_browser_id: str = Header(None)):
    if not x_browser_id:
        return {"ok": False, "error": "Missing browser id."}
    return ssh_mgr.disconnect(body.device_id, x_browser_id)


@app.post("/api/ssh/disconnect_all")
async def ssh_disconnect_all():
    return ssh_mgr.disconnect_all()


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str
    command:   Annotated[str, Field(max_length=MAX_COMMAND_LEN)]
    use_sudo:  bool = False
    label:     Optional[Annotated[str, Field(max_length=MAX_NAME_LEN)]] = None
    cmd_id:    Optional[str] = None


@app.post("/api/ssh/run")
async def ssh_run(body: RunIn, x_browser_id: str = Header(None)):
    if not x_browser_id:
        return {"ok": False, "error": "Missing browser id."}
    return ssh_mgr.run_command(body.device_id, body.command, body.use_sudo, body.label, x_browser_id, cmd_id=body.cmd_id)


@app.post("/api/ssh/cancel")
async def ssh_cancel(body: DeviceIdIn, x_browser_id: str = Header(None)):
    if not x_browser_id:
        return {"ok": False, "error": "Missing browser id."}
    return ssh_mgr.cancel(body.device_id, x_browser_id)


class TrustIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str


@app.post("/api/ssh/trust_key")
async def ssh_trust_key(body: TrustIn):
    return ssh_mgr.trust_host_key(body.device_id)


@app.get("/api/ssh/host_key/{host}")
async def ssh_host_key(host: str):
    return ssh_mgr.get_host_key(host)


@app.delete("/api/ssh/host_key/{host}")
async def ssh_forget_key(host: str):
    return ssh_mgr.forget_host_key(host)


# ---------------------------------------------------------------------------
# Debug log
# ---------------------------------------------------------------------------

class DebugIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


@app.post("/api/debug")
async def toggle_debug(body: DebugIn):
    global _debug_file
    if body.enabled and _debug_file is None:
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path  = os.path.join(LOG_DIR, f"Debug_Log_{stamp}.txt")
        _debug_file = _open_private_file(path, buffering=1)  # line-buffered
        _debug_write("=== Debug log started ===")
        return {"ok": True, "enabled": True, "path": path}
    if not body.enabled and _debug_file is not None:
        _debug_write("=== Debug log stopped ===")
        _debug_file.close()
        _debug_file = None
    return {"ok": True, "enabled": False}


# ---------------------------------------------------------------------------
# Update check
# ---------------------------------------------------------------------------

GITHUB_RELEASES_URL = (
    "https://api.github.com/repos/JDE-Projects/Simple-Network-Dashboard/releases/latest"
)


def _update_error_reason(exc: BaseException) -> str:
    """Turn a check_update exception into a short, plain-language reason to
    show in the UI. Pure and network-free: takes the already-raised exception,
    never touches the network itself.

    Each branch is specific to a failure that can actually cause it, and
    names a next step where there is a sensible one. Subclasses are checked
    before their parents: SSLCertVerificationError and SSLEOFError/
    SSLZeroReturnError before the generic ssl.SSLError, and the specific
    ConnectionError subclasses and socket.gaierror before the generic OSError
    branch (socket.timeout is an alias of TimeoutError, and both are OSError
    subclasses)."""
    # HTTPError is a URLError subclass but carries its own .code, so classify
    # it before unwrapping anything.
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 403:
            return (
                "GitHub is rate-limiting update checks from this network. "
                "Try again later."
            )
        if exc.code == 404:
            return "No published release was found."
        if 500 <= exc.code < 600:
            return f"GitHub is having trouble on its end (HTTP {exc.code})."
        return f"GitHub returned an error (HTTP {exc.code})."

    if isinstance(exc, json.JSONDecodeError):
        return (
            "GitHub returned something unexpected. This often means a proxy "
            "or a guest wifi sign-in page answered instead."
        )

    # A plain URLError wraps the underlying cause (ssl.SSLError, socket.timeout,
    # a DNS/socket OSError, ...) in its .reason; unwrap it to classify the
    # actual cause, but remember it came from a URLError for the fallback below.
    is_url_error = isinstance(exc, urllib.error.URLError)
    cause = exc.reason if is_url_error and exc.reason is not None else exc

    if isinstance(cause, ssl.SSLCertVerificationError):
        return (
            "GitHub's certificate could not be verified. This usually means "
            "antivirus or a network filter is inspecting HTTPS traffic."
        )
    if isinstance(cause, (ssl.SSLEOFError, ssl.SSLZeroReturnError)):
        return "The secure connection was cut off during the handshake with GitHub."
    if isinstance(cause, ssl.SSLError):
        return "The secure connection to GitHub failed."
    if isinstance(cause, socket.gaierror):
        return (
            "The address for api.github.com could not be looked up. Check "
            "DNS or the internet connection."
        )
    if isinstance(cause, (socket.timeout, TimeoutError)):
        return "GitHub didn't respond in time."
    if isinstance(cause, (ConnectionRefusedError, ConnectionResetError)):
        return (
            "The connection was refused or reset. A firewall or proxy may "
            "be blocking it."
        )
    if isinstance(cause, OSError) and getattr(cause, "errno", None) == errno.ENETUNREACH:
        return "No network connection."
    if is_url_error:
        return "Couldn't reach GitHub. Check the internet connection."

    text = f"{type(exc).__name__}: {exc}"
    if len(text) > 120:
        text = text[:117] + "..."
    return text


def _version_tuple(v: str) -> tuple:
    """Turn '1.2.3' into (1, 2, 3).  Non-numeric parts default to 0."""
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _fetch_latest_version() -> str:
    """Blocking call — must be run in an executor.  Returns the latest release version (no leading 'v')."""
    req = urllib.request.Request(
        GITHUB_RELEASES_URL,
        headers={
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("tag_name") or "").lstrip("vV")


@app.get("/api/check-update")
async def check_update():
    try:
        loop = asyncio.get_running_loop()
        latest = await loop.run_in_executor(None, _fetch_latest_version)
        return {
            "ok": True,
            "current": APP_VERSION,
            "latest": latest,
            "update_available": _version_tuple(latest) > _version_tuple(APP_VERSION),
        }
    except Exception as e:
        reason = _update_error_reason(e)
        try:
            _debug_write(f"check_update failed: {type(e).__name__}: {e}")
        except Exception:
            pass
        return {"ok": False, "reason": reason}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_port(argv: list) -> int:
    """Pull --port N / --port=N out of the CLI args.  Defaults to 3000 when
    absent.  Invalid values exit loudly instead of silently falling back."""
    port = 3000
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--port":
            if i + 1 >= len(argv):
                sys.exit("ERROR: --port requires a value.")
            raw = argv[i + 1]
            i += 2
        elif arg.startswith("--port="):
            raw = arg[len("--port="):]
            i += 1
        else:
            i += 1
            continue
        try:
            port = int(raw)
        except ValueError:
            sys.exit(f"ERROR: --port requires a numeric port, got '{raw}'.")
        if not (1 <= port <= 65535):
            sys.exit(f"ERROR: --port must be between 1 and 65535, got {port}.")
    return port


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=_parse_port(sys.argv[1:]), reload=False)
