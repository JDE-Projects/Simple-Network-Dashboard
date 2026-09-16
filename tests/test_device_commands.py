"""Task 5 Phase 1 device API command-field semantics.

POST /api/devices must tell apart three distinct meanings of the
``commands`` field: omitted (preserve on update, empty on create),
an explicit empty list (clear the library), and a non-empty list
(replace the library).
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
import main
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
    """Replace on-disk persistence with an in-memory list so the test only
    exercises the merge/clear/replace decision under upsert_device."""
    monkeypatch.setattr(main, "_devices_cache", copy.deepcopy(seed_devices))

    def _fake_save(devices: list) -> bool:
        main._devices_cache = list(devices)
        return True

    monkeypatch.setattr(main, "_save", _fake_save)

    async def _noop_broadcast(_message):
        return None

    monkeypatch.setattr(main.ws_mgr, "broadcast", _noop_broadcast)


def _device(command_names: list[str]) -> dict:
    return {
        "id":           "dev_existing",
        "name":         "Existing Device",
        "host":         "10.0.0.5",
        "username":     "pi",
        "metrics_port": 9100,
        "commands": [
            {"name": name, "command": f"echo {name}", "sudo": False, "confirm": "", "pinned": False}
            for name in command_names
        ],
    }


def test_update_with_commands_omitted_preserves_existing_commands(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [_device(["uptime", "df -h"])])

    response = client.post(
        "/api/devices",
        json={
            "id": "dev_existing", "name": "Existing Device",
            "host": "10.0.0.5", "username": "pi", "metrics_port": 9100,
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    saved = response.json()["devices"][0]
    assert [c["name"] for c in saved["commands"]] == ["uptime", "df -h"]


def test_update_with_empty_commands_clears_the_library(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [_device(["uptime", "df -h"])])

    response = client.post(
        "/api/devices",
        json={
            "id": "dev_existing", "name": "Existing Device",
            "host": "10.0.0.5", "username": "pi", "metrics_port": 9100,
            "commands": [],
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    saved = response.json()["devices"][0]
    assert saved["commands"] == []


def test_update_with_new_commands_replaces_the_library(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [_device(["uptime", "df -h"])])

    response = client.post(
        "/api/devices",
        json={
            "id": "dev_existing", "name": "Existing Device",
            "host": "10.0.0.5", "username": "pi", "metrics_port": 9100,
            "commands": [{"name": "reboot", "command": "sudo reboot", "sudo": True, "confirm": "", "pinned": False}],
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    saved = response.json()["devices"][0]
    assert [c["name"] for c in saved["commands"]] == ["reboot"]


def test_new_device_with_commands_omitted_saves_empty_library(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json={"name": "New Device", "host": "10.0.0.6", "username": "pi", "metrics_port": 9100},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    devices = response.json()["devices"]
    assert len(devices) == 1
    assert devices[0]["commands"] == []


def test_clearing_the_final_command_leaves_zero_commands(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [_device(["uptime"])])

    response = client.post(
        "/api/devices",
        json={
            "id": "dev_existing", "name": "Existing Device",
            "host": "10.0.0.5", "username": "pi", "metrics_port": 9100,
            "commands": [],
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    saved = response.json()["devices"][0]
    assert saved["commands"] == []
