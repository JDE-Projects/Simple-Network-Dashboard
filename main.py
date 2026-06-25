"""
Simple Network Dashboard — FastAPI backend.

Serves the static UI, exposes REST endpoints for device/SSH management,
and pushes real-time metrics + SSH events over a WebSocket.
"""

import asyncio
import json
import os
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from metrics_poller import fetch_metrics
from ssh_manager import SSHManager

METRICS_INTERVAL = 2  # seconds between polls for the selected device

APP_NAME    = "Simple Network Dashboard"
APP_VERSION = "1.2.0"
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DEVICES_FILE = os.path.join(BASE_DIR, "devices.json")


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

class _WSManager:
    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)

    def drop(self, ws: WebSocket):
        self._connections = [c for c in self._connections if c is not ws]

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


ws_mgr = _WSManager()

# Latest metrics per device (includes _raw fields for delta calculation)
_metrics_cache: dict[str, dict] = {}

# Device ID currently selected in the browser (None = no device selected / no clients)
_selected_device_id: Optional[str] = None

# In-memory device list — seeded from disk at startup, kept in sync by _save()
_devices_cache: list = []

# Debug log file handle — None when disabled
_debug_file = None


def _debug_write(text: str):
    if _debug_file is not None:
        stamp = datetime.now().strftime("%H:%M:%S")
        _debug_file.write(f"[{stamp}] {text}\n")


async def _broadcast(msg: dict):
    """Broadcast wrapper that also writes SSH events to the debug log."""
    if _debug_file is not None:
        t   = msg.get("type", "")
        did = msg.get("device_id", "")
        if t == "ssh_log":
            _debug_write(f"SSH [{did}] {msg.get('level', 'out').upper()}: {msg.get('text', '')}")
        elif t == "ssh_status":
            _debug_write(f"SSH [{did}] → {msg.get('state', '')}")
    await ws_mgr.broadcast(msg)


ssh_mgr = SSHManager(_broadcast)


# ---------------------------------------------------------------------------
# Device persistence
# ---------------------------------------------------------------------------

def _load() -> list:
    if not os.path.exists(DEVICES_FILE):
        return []
    try:
        with open(DEVICES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        devices = data.get("devices", data) if isinstance(data, dict) else data
        return [_norm(d) for d in devices if isinstance(d, dict)]
    except Exception:
        return []


def _save(devices: list) -> bool:
    global _devices_cache
    try:
        with open(DEVICES_FILE, "w", encoding="utf-8") as f:
            json.dump({"_app": APP_NAME, "devices": devices}, f, indent=2)
        _devices_cache = list(devices)
        return True
    except Exception:
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
            "pinned":  bool(c.get("pinned", False)),
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


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _devices_cache
    _devices_cache = _load()
    ssh_mgr.set_loop(asyncio.get_running_loop())
    task = asyncio.create_task(_metrics_loop())
    yield
    task.cancel()
    ssh_mgr.disconnect_all()
    if _debug_file is not None:
        _debug_write("=== Debug log closed (server shutdown) ===")
        _debug_file.close()


app = FastAPI(title=APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.get("/")
async def root():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws_mgr.connect(ws)
    try:
        # Push current state so a fresh page load (or reconnect) is in sync
        devices = _load()
        await ws.send_text(json.dumps({
            "type": "init", "devices": devices, "version": APP_VERSION,
            "ssh_connected": list(ssh_mgr.sessions.keys()),
            "debug": _debug_file is not None,
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
    except WebSocketDisconnect:
        pass
    finally:
        ws_mgr.drop(ws)


# ---------------------------------------------------------------------------
# Device CRUD
# ---------------------------------------------------------------------------

class DeviceIn(BaseModel):
    id:           Optional[str] = None
    name:         str
    host:         str
    username:     str
    metrics_port: int  = 9100
    commands:     list = []


@app.get("/api/devices")
async def get_devices():
    return _load()


@app.post("/api/devices")
async def upsert_device(body: DeviceIn):
    d       = body.model_dump()
    devices = _load()
    if d.get("id"):
        for i, existing in enumerate(devices):
            if existing["id"] == d["id"]:
                # Preserve the existing command list unless the caller sent one
                if not d["commands"]:
                    d["commands"] = existing.get("commands", [])
                devices[i] = _norm(d)
                break
        else:
            devices.append(_norm(d))
    else:
        d["id"] = "dev_" + uuid.uuid4().hex[:12]
        devices.append(_norm(d))
    _save(devices)
    await ws_mgr.broadcast({"type": "devices", "devices": devices})
    return {"ok": True, "devices": devices}


@app.delete("/api/devices/{device_id}")
async def delete_device(device_id: str):
    global _selected_device_id
    if _selected_device_id == device_id:
        _selected_device_id = None
    devices = [d for d in _load() if d["id"] != device_id]
    _save(devices)
    ssh_mgr.disconnect(device_id)
    await ws_mgr.broadcast({"type": "devices", "devices": devices})
    return {"ok": True, "devices": devices}


class CommandsIn(BaseModel):
    commands: list


@app.put("/api/devices/{device_id}/commands")
async def update_commands(device_id: str, body: CommandsIn):
    devices = _load()
    for i, d in enumerate(devices):
        if d["id"] == device_id:
            d["commands"] = body.commands
            devices[i] = _norm(d)
            break
    _save(devices)
    await ws_mgr.broadcast({"type": "devices", "devices": devices})
    return {"ok": True, "devices": devices}


# ---------------------------------------------------------------------------
# SSH endpoints
# ---------------------------------------------------------------------------

class ConnectIn(BaseModel):
    device_id: str
    password:  str


@app.post("/api/ssh/connect")
async def ssh_connect(body: ConnectIn):
    devices = _load()
    device  = next((d for d in devices if d["id"] == body.device_id), None)
    if not device:
        return {"ok": False, "error": "Device not found."}
    if not body.password:
        return {"ok": False, "error": "Password is required."}
    loop   = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, ssh_mgr.connect, body.device_id, body.password, device)
    return result


class DeviceIdIn(BaseModel):
    device_id: str


@app.post("/api/ssh/disconnect")
async def ssh_disconnect(body: DeviceIdIn):
    return ssh_mgr.disconnect(body.device_id)


@app.post("/api/ssh/disconnect_all")
async def ssh_disconnect_all():
    return ssh_mgr.disconnect_all()


class RunIn(BaseModel):
    device_id: str
    command:   str
    use_sudo:  bool = False
    label:     Optional[str] = None


@app.post("/api/ssh/run")
async def ssh_run(body: RunIn):
    return ssh_mgr.run_command(body.device_id, body.command, body.use_sudo, body.label)


@app.post("/api/ssh/cancel")
async def ssh_cancel(body: DeviceIdIn):
    return ssh_mgr.cancel(body.device_id)


class TrustIn(BaseModel):
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
    enabled: bool


@app.post("/api/debug")
async def toggle_debug(body: DebugIn):
    global _debug_file
    if body.enabled and _debug_file is None:
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path  = os.path.join(BASE_DIR, f"Debug_Log_{stamp}.txt")
        _debug_file = open(path, "w", encoding="utf-8", buffering=1)  # line-buffered
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
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["tag_name"].lstrip("vV")


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
    except Exception:
        return {"ok": False}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=3000, reload=False)
