"""Device API command-field semantics.

POST /api/devices must tell apart three distinct meanings of the
``commands`` field: omitted (preserve on update, empty on create),
an explicit empty list (clear the library), and a non-empty list
(replace the library).
"""

from __future__ import annotations

import copy
from pathlib import Path

from fastapi.testclient import TestClient

from simple_network_dashboard import auth, main, persistence, runtime_state
from simple_network_dashboard.session_manager import LoginThrottle, SessionStore
from simple_network_dashboard.ssh_session import _Session


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
    monkeypatch.setattr(runtime_state, "_devices_cache", copy.deepcopy(seed_devices))

    def _fake_save(devices: list) -> bool:
        runtime_state._devices_cache = list(devices)
        return True

    monkeypatch.setattr(persistence, "_save", _fake_save)

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
            {"name": name, "command": f"echo {name}", "sudo": False, "confirm": ""}
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
            "commands": [{"name": "reboot", "command": "sudo reboot", "sudo": True, "confirm": ""}],
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


# ---------------------------------------------------------------------------
# Host and username edits while the device has an SSH session
# ---------------------------------------------------------------------------

_SAME_OWNER_ERROR = "Disconnect this device before changing its host or username."
_OTHER_OWNER_ERROR = ("This device is in use by another session. Its host and "
                      "username can't be changed until that session disconnects.")


def _own_owner(client: TestClient) -> str:
    return main._session_owner(client.cookies[main.SESSION_COOKIE_NAME])


def _seed_session(monkeypatch, owner: str, state: str) -> None:
    session = _Session(_device([]), "pw", owner, "dev_existing")
    session.state = state
    monkeypatch.setattr(main.ssh_mgr, "sessions", {"dev_existing": session})
    monkeypatch.setattr(main.ssh_mgr, "_identity_edits", set())


def _post_edit(client: TestClient, **changes) -> dict:
    body = {
        "id": "dev_existing", "name": "Existing Device",
        "host": "10.0.0.5", "username": "pi", "metrics_port": 9100,
    }
    body.update(changes)
    response = client.post("/api/devices", json=body, headers=_csrf_headers(client))
    assert response.status_code == 200
    return response.json()


def _track_saves(monkeypatch) -> list:
    calls: list = []
    real = persistence._save

    def counting(devices):
        calls.append(devices)
        return real(devices)

    monkeypatch.setattr(persistence, "_save", counting)
    return calls


def test_host_change_refused_while_connected_same_owner(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [_device([])])
    _seed_session(monkeypatch, _own_owner(client), "connected")
    calls = _track_saves(monkeypatch)

    result = _post_edit(client, host="10.0.0.99")

    assert result == {"ok": False, "error": _SAME_OWNER_ERROR}
    assert runtime_state._devices_cache[0]["host"] == "10.0.0.5"
    assert calls == []


def test_username_change_refused_while_connected_same_owner(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [_device([])])
    _seed_session(monkeypatch, _own_owner(client), "connected")
    calls = _track_saves(monkeypatch)

    result = _post_edit(client, username="root")

    assert result == {"ok": False, "error": _SAME_OWNER_ERROR}
    assert runtime_state._devices_cache[0]["username"] == "pi"
    assert calls == []


def test_identity_change_refused_while_connecting(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [_device([])])
    _seed_session(monkeypatch, _own_owner(client), "connecting")
    calls = _track_saves(monkeypatch)

    assert _post_edit(client, host="10.0.0.99") == {"ok": False, "error": _SAME_OWNER_ERROR}
    assert calls == []


def test_identity_change_refused_while_host_key_pending(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [_device([])])
    _seed_session(monkeypatch, _own_owner(client), "host_key_pending")
    calls = _track_saves(monkeypatch)

    assert _post_edit(client, host="10.0.0.99") == {"ok": False, "error": _SAME_OWNER_ERROR}
    assert calls == []


def test_identity_change_refused_with_other_owner_wording(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [_device([])])
    _seed_session(monkeypatch, "some-other-owner", "connected")
    calls = _track_saves(monkeypatch)

    assert _post_edit(client, host="10.0.0.99") == {"ok": False, "error": _OTHER_OWNER_ERROR}
    assert runtime_state._devices_cache[0]["host"] == "10.0.0.5"
    assert calls == []


def test_name_and_metrics_port_edits_succeed_while_connected(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [_device([])])
    _seed_session(monkeypatch, _own_owner(client), "connected")

    result = _post_edit(client, name="Renamed", metrics_port=9200)

    assert result["ok"] is True
    saved = runtime_state._devices_cache[0]
    assert saved["name"] == "Renamed"
    assert saved["metrics_port"] == 9200


def test_host_change_succeeds_without_a_session_and_releases_the_guard(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [_device([])])
    monkeypatch.setattr(main.ssh_mgr, "sessions", {})
    monkeypatch.setattr(main.ssh_mgr, "_identity_edits", set())

    result = _post_edit(client, host="10.0.0.99")

    assert result["ok"] is True
    assert runtime_state._devices_cache[0]["host"] == "10.0.0.99"
    assert main.ssh_mgr._identity_edits == set()


def test_guard_is_released_when_save_returns_false(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [_device([])])
    monkeypatch.setattr(main.ssh_mgr, "sessions", {})
    monkeypatch.setattr(main.ssh_mgr, "_identity_edits", set())
    monkeypatch.setattr(persistence, "_save", lambda _devices: False)

    result = _post_edit(client, host="10.0.0.99")

    assert result["ok"] is False
    assert main.ssh_mgr._identity_edits == set()


def test_guard_is_released_when_save_raises(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [_device([])])
    monkeypatch.setattr(main.ssh_mgr, "sessions", {})
    monkeypatch.setattr(main.ssh_mgr, "_identity_edits", set())

    def boom(_devices):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(persistence, "_save", boom)
    raising_client = TestClient(main.app, base_url="https://testserver", raise_server_exceptions=False)
    raising_client.cookies.update(client.cookies)
    response = raising_client.post(
        "/api/devices",
        json={"id": "dev_existing", "name": "Existing Device", "host": "10.0.0.99",
              "username": "pi", "metrics_port": 9100},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 500
    assert main.ssh_mgr._identity_edits == set()


def test_half_failed_save_still_broadcasts_the_replaced_list(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [_device([])])
    monkeypatch.setattr(main.ssh_mgr, "sessions", {})
    monkeypatch.setattr(main.ssh_mgr, "_identity_edits", set())

    def half_failed(devices):
        runtime_state._devices_cache = list(devices)
        return False

    monkeypatch.setattr(persistence, "_save", half_failed)
    sent: list = []

    async def capture(message):
        sent.append(message)

    monkeypatch.setattr(main.ws_mgr, "broadcast", capture)

    result = _post_edit(client, host="10.0.0.99")

    assert result == {"ok": False, "error": main._SAVE_ERROR}
    assert sent == [{"type": "devices", "devices": runtime_state._devices_cache}]
    assert sent[0]["devices"][0]["host"] == "10.0.0.99"
