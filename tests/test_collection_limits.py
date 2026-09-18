"""Collection size caps: at most 250 saved commands per device, per request,
and at most 250 devices total.

Phase 2 of request validation hardening adds two limits to main.py:
a Pydantic field cap on the ``commands`` list carried by POST /api/devices
and PUT /api/devices/{id}/commands, and a device-count cap enforced in the
upsert_device handler that only blocks creating a brand-new device once the
store already holds 250.
"""

from __future__ import annotations

import copy
from pathlib import Path

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
    monkeypatch.setattr(main, "_devices_cache", copy.deepcopy(seed_devices))

    def _fake_save(devices: list) -> bool:
        main._devices_cache = list(devices)
        return True

    monkeypatch.setattr(main, "_save", _fake_save)

    async def _noop_broadcast(_message):
        return None

    monkeypatch.setattr(main.ws_mgr, "broadcast", _noop_broadcast)


def _make_devices(count: int) -> list:
    return [
        {
            "id": f"dev_{i:04d}",
            "name": f"Device {i}",
            "host": f"10.0.0.{i % 250}",
            "username": "pi",
            "metrics_port": 9100,
            "commands": [],
        }
        for i in range(count)
    ]


def _command_list(count: int) -> list:
    return [{"name": f"c{i}", "command": f"echo {i}"} for i in range(count)]


def test_exactly_250_commands_accepted(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json={
            "name": "New Device", "host": "10.0.0.6", "username": "pi",
            "metrics_port": 9100, "commands": _command_list(250),
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert len(body["devices"][0]["commands"]) == 250


def test_251_commands_rejected_with_app_error_shape(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json={
            "name": "New Device", "host": "10.0.0.6", "username": "pi",
            "metrics_port": 9100, "commands": _command_list(251),
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "error": "The request was not valid."}


def test_251_commands_rejected_on_put_commands_endpoint(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, _make_devices(1))

    response = client.put(
        "/api/devices/dev_0000/commands",
        json={"commands": _command_list(251)},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "error": "The request was not valid."}


def test_creating_device_at_cap_is_rejected(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    seed = _make_devices(250)
    _stub_storage(monkeypatch, seed)

    response = client.post(
        "/api/devices",
        json={"name": "One Too Many", "host": "10.0.0.250", "username": "pi", "metrics_port": 9100},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.json() == {"ok": False, "error": main._DEVICE_LIMIT_ERROR}
    assert len(main._devices_cache) == 250


def test_updating_existing_device_at_cap_still_succeeds(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    seed = _make_devices(250)
    _stub_storage(monkeypatch, seed)

    response = client.post(
        "/api/devices",
        json={
            "id": "dev_0000", "name": "Renamed Device", "host": "10.0.0.0",
            "username": "pi", "metrics_port": 9100,
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert len(body["devices"]) == 250
    updated = next(d for d in body["devices"] if d["id"] == "dev_0000")
    assert updated["name"] == "Renamed Device"


def test_normal_small_request_under_both_caps_succeeds(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json={
            "name": "New Device", "host": "10.0.0.6", "username": "pi",
            "metrics_port": 9100, "commands": _command_list(3),
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert len(body["devices"]) == 1
    assert len(body["devices"][0]["commands"]) == 3
