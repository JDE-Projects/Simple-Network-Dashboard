"""Server-owned device IDs.

Phase 4 of request validation hardening keeps device IDs server-generated.
POST /api/devices with an ``id`` that matches an existing device updates it,
but an ``id`` that matches nothing is rejected and must not create a new
device with the caller's chosen ID. A request with no ``id`` still creates a
device with a server-assigned ID.
"""

from __future__ import annotations

import copy
from pathlib import Path

from fastapi.testclient import TestClient

import auth
import main
import persistence
import runtime_state
from session_manager import LoginThrottle, SessionStore


def _csrf_headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(main.CSRF_COOKIE_NAME)
    assert token
    return {"X-CSRF-Token": token}


def _authenticated_client(tmp_path: Path, monkeypatch) -> TestClient:
    auth_file = tmp_path / "auth.json"
    auth.publish_auth_state(auth_file, auth.create_auth_state("a" * 15))
    monkeypatch.setattr(main, "AUTH_FILE", str(auth_file))
    monkeypatch.setattr(main, "_session_store", SessionStore(tmp_path / "sessions.json"))
    monkeypatch.setattr(main, "_login_throttle", LoginThrottle())
    client = TestClient(main.app, base_url="https://testserver")
    assert client.get("/login").status_code == 200
    response = client.post(
        "/api/auth/login",
        json={"password": "a" * 15, "remembered": False},
        headers=_csrf_headers(client),
    )
    assert response.status_code == 200
    return client


def _stub_storage(monkeypatch, seed_devices: list) -> None:
    monkeypatch.setattr(runtime_state, "_devices_cache", copy.deepcopy(seed_devices))

    def _fake_save(devices: list) -> bool:
        runtime_state._devices_cache = list(devices)
        return True

    monkeypatch.setattr(persistence, "_save", _fake_save)

    async def _noop_broadcast(_message):
        return None

    monkeypatch.setattr(main.ws_mgr, "broadcast", _noop_broadcast)


def _device(device_id: str, name: str = "Existing") -> dict:
    return {
        "id": device_id,
        "name": name,
        "host": "10.0.0.5",
        "username": "pi",
        "metrics_port": 9100,
        "commands": [],
    }


def test_unknown_id_is_rejected_and_creates_nothing(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [_device("dev_existing0001")])

    response = client.post(
        "/api/devices",
        json={
            "id": "dev_chosenbyme01",
            "name": "Injected",
            "host": "10.0.0.9",
            "username": "pi",
            "metrics_port": 9100,
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.json() == {"ok": False, "error": main._UNKNOWN_DEVICE_ERROR}
    # Store still holds only the original device; nothing with the chosen ID.
    ids = [d["id"] for d in runtime_state._devices_cache]
    assert ids == ["dev_existing0001"]


def test_existing_id_still_updates(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [_device("dev_existing0001", name="Old")])

    response = client.post(
        "/api/devices",
        json={
            "id": "dev_existing0001",
            "name": "Renamed",
            "host": "10.0.0.5",
            "username": "pi",
            "metrics_port": 9100,
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert [d["name"] for d in body["devices"]] == ["Renamed"]


def test_no_id_creates_with_server_assigned_id(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json={"name": "New", "host": "10.0.0.7", "username": "pi", "metrics_port": 9100},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert len(body["devices"]) == 1
    assert body["devices"][0]["id"].startswith("dev_")
