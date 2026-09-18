"""Field-level character and range limits on saved devices, saved commands,
and run requests.

Phase 3 of request validation hardening adds maximum lengths (and a port
range) to the fields the browser sends, so an over-limit value is rejected
by Pydantic validation instead of being silently truncated.
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


def _base_device(**overrides) -> dict:
    device = {
        "name": "New Device",
        "host": "10.0.0.6",
        "username": "pi",
        "metrics_port": 9100,
    }
    device.update(overrides)
    return device


_INVALID = {"ok": False, "error": "The request was not valid."}


def test_device_name_at_80_chars_accepted(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json=_base_device(name="n" * 80),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_device_name_over_80_chars_rejected(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json=_base_device(name="n" * 81),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    assert response.json() == _INVALID


def test_host_at_253_chars_accepted(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json=_base_device(host="h" * 253),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_host_over_253_chars_rejected(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json=_base_device(host="h" * 254),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    assert response.json() == _INVALID


def test_username_at_64_chars_accepted(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json=_base_device(username="u" * 64),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_username_over_64_chars_rejected(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json=_base_device(username="u" * 65),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    assert response.json() == _INVALID


def test_metrics_port_1_and_65535_accepted(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    for port in (1, 65535):
        response = client.post(
            "/api/devices",
            json=_base_device(metrics_port=port),
            headers=_csrf_headers(client),
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True


def test_metrics_port_0_and_65536_rejected(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    for port in (0, 65536):
        response = client.post(
            "/api/devices",
            json=_base_device(metrics_port=port),
            headers=_csrf_headers(client),
        )
        assert response.status_code == 422
        assert response.json() == _INVALID


def test_command_name_at_80_chars_accepted(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json=_base_device(commands=[{"name": "c" * 80, "command": "echo hi"}]),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_command_name_over_80_chars_rejected(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json=_base_device(commands=[{"name": "c" * 81, "command": "echo hi"}]),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    assert response.json() == _INVALID


def test_command_text_at_4096_chars_accepted(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json=_base_device(commands=[{"name": "c", "command": "e" * 4096}]),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_command_text_over_4096_chars_rejected(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json=_base_device(commands=[{"name": "c", "command": "e" * 4097}]),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    assert response.json() == _INVALID


def test_command_confirm_at_500_chars_accepted(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json=_base_device(commands=[{"name": "c", "command": "echo hi", "confirm": "x" * 500}]),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_command_confirm_over_500_chars_rejected(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json=_base_device(commands=[{"name": "c", "command": "echo hi", "confirm": "x" * 501}]),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    assert response.json() == _INVALID


def test_command_item_with_extra_key_rejected(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json=_base_device(commands=[{"name": "c", "command": "echo hi", "unexpected": "nope"}]),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    assert response.json() == _INVALID


def test_field_limits_also_enforced_on_put_commands_endpoint(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [
        {
            "id": "dev_0000", "name": "Device", "host": "10.0.0.1",
            "username": "pi", "metrics_port": 9100, "commands": [],
        },
    ])

    response = client.put(
        "/api/devices/dev_0000/commands",
        json={"commands": [{"name": "c", "command": "e" * 4097}]},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    assert response.json() == _INVALID


def test_normal_valid_device_and_commands_request_succeeds(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json=_base_device(commands=[
            {"name": "Uptime", "command": "uptime", "sudo": False, "confirm": ""},
        ]),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert len(body["devices"]) == 1
    assert body["devices"][0]["commands"][0]["name"] == "Uptime"

    device_id = body["devices"][0]["id"]
    response = client.put(
        f"/api/devices/{device_id}/commands",
        json={"commands": [{"name": "Disk", "command": "df -h"}]},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    updated = next(d for d in body["devices"] if d["id"] == device_id)
    assert updated["commands"][0]["name"] == "Disk"
