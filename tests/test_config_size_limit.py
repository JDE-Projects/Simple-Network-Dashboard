"""Total saved configuration size cap.

POST /api/devices and PUT /api/devices/{id}/commands reject a save that would
push the whole device list past MAX_CONFIG_BYTES, unless the save makes an
already-oversized list smaller. The loader still accepts an oversized file.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from fastapi.testclient import TestClient

from simple_network_dashboard import config, main, persistence, runtime_state
from tests.test_collection_limits import _authenticated_client, _csrf_headers, _make_devices, _stub_storage

BIG = 3000


def _commands(count: int, size: int = BIG) -> list:
    return [{"name": f"c{i}", "command": "x" * size, "sudo": False, "confirm": ""} for i in range(count)]


def _size(devices: list) -> int:
    return len(json.dumps(devices))


def _near_cap_devices() -> list:
    """Two devices whose total is under the cap with less than 3 KB to spare."""
    devices = _make_devices(2)
    devices[0]["commands"] = _commands(250)
    count = 0
    while _size(devices) + BIG + 200 < config.MAX_CONFIG_BYTES:
        count += 1
        devices[1]["commands"] = _commands(count)
    while config.MAX_CONFIG_BYTES - _size(devices) >= 3000:
        devices[1]["commands"].append({"name": "p", "command": "y" * 100, "sudo": False, "confirm": ""})
    devices = [persistence._norm(d) for d in devices]
    assert _size(devices) <= config.MAX_CONFIG_BYTES
    return devices


def _over_cap_devices() -> list:
    devices = _near_cap_devices()
    devices[1]["commands"] += _commands(100)
    devices = [persistence._norm(d) for d in devices]
    assert _size(devices) > config.MAX_CONFIG_BYTES
    return devices


def _track_saves(monkeypatch) -> list:
    calls: list = []
    real = persistence._save

    def counting(devices):
        calls.append(devices)
        return real(devices)

    monkeypatch.setattr(persistence, "_save", counting)
    return calls


def _put(client, device_id: str, commands: list) -> dict:
    response = client.put(
        f"/api/devices/{device_id}/commands",
        json={"commands": commands},
        headers=_csrf_headers(client),
    )
    assert response.status_code == 200
    return response.json()


def _post(client, body: dict) -> dict:
    response = client.post("/api/devices", json=body, headers=_csrf_headers(client))
    assert response.status_code == 200
    return response.json()


def _device_body(device: dict, **changes) -> dict:
    body = {k: device[k] for k in ("id", "name", "host", "username", "metrics_port")}
    body.update(changes)
    return body


def test_save_under_cap_succeeds(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, _make_devices(2))
    result = _put(client, "dev_0000", _commands(5))
    assert result["ok"] is True
    assert len(runtime_state._devices_cache[0]["commands"]) == 5


def test_put_and_post_that_cross_the_cap_are_rejected(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    seed = _near_cap_devices()
    _stub_storage(monkeypatch, seed)
    saves = _track_saves(monkeypatch)
    cache_before = runtime_state._devices_cache
    snapshot = copy.deepcopy(cache_before)

    grown = seed[1]["commands"] + _commands(1, 4000)
    assert _put(client, "dev_0001", grown) == {"ok": False, "error": main._CONFIG_SIZE_ERROR}
    assert _post(client, _device_body(seed[1], commands=grown)) == {
        "ok": False, "error": main._CONFIG_SIZE_ERROR}

    new_device = {"name": "New", "host": "10.9.9.9", "username": "pi", "metrics_port": 9100,
                  "commands": _commands(1, 4000)}
    assert _post(client, new_device) == {"ok": False, "error": main._CONFIG_SIZE_ERROR}

    assert main._CONFIG_SIZE_ERROR == (
        "This change would make the saved configuration larger than the 1 MB limit. "
        "Remove or shorten some saved commands and try again.")
    assert saves == []
    assert runtime_state._devices_cache is cache_before
    assert runtime_state._devices_cache == snapshot


def test_rejection_logs_sizes_without_command_text(tmp_path: Path, monkeypatch, capsys) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    seed = _near_cap_devices()
    _stub_storage(monkeypatch, seed)
    _put(client, "dev_0001", seed[1]["commands"] + [{"name": "n", "command": "SECRET" * 600}])
    out = capsys.readouterr().out
    assert "SAVE REJECTED" in out
    assert "SECRET" not in out


def test_over_cap_configuration_can_only_shrink(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    seed = _over_cap_devices()
    _stub_storage(monkeypatch, seed)
    commands = seed[1]["commands"]

    assert _put(client, "dev_0001", commands)["ok"] is False
    assert _put(client, "dev_0001", commands + _commands(1))["ok"] is False
    assert len(runtime_state._devices_cache[1]["commands"]) == len(commands)

    shrunk = _put(client, "dev_0001", commands[:-1])
    assert shrunk["ok"] is True
    assert len(runtime_state._devices_cache[1]["commands"]) == len(commands) - 1


def test_identity_edit_over_cap_leaves_no_claim(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    seed = _near_cap_devices()
    _stub_storage(monkeypatch, seed)
    saves = _track_saves(monkeypatch)
    claims: list = []
    real_begin = main.ssh_mgr.begin_identity_edit

    def spy(device_id, owner):
        claims.append(device_id)
        return real_begin(device_id, owner)

    monkeypatch.setattr(main.ssh_mgr, "begin_identity_edit", spy)

    body = _device_body(seed[1], host="10.8.8.8", commands=seed[1]["commands"] + _commands(1, 4000))
    assert _post(client, body) == {"ok": False, "error": main._CONFIG_SIZE_ERROR}
    assert claims == []
    assert main.ssh_mgr._identity_edits == set()
    assert saves == []


def test_loader_accepts_an_over_cap_file(tmp_path: Path, monkeypatch) -> None:
    devices = _over_cap_devices()
    devices_file = tmp_path / "devices.json"
    devices_file.write_text(
        json.dumps({"_app": config.APP_NAME, "devices": devices}), encoding="utf-8")
    monkeypatch.setattr(persistence, "DEVICES_FILE", str(devices_file))
    monkeypatch.setattr(runtime_state, "_recovery_mode", False)

    loaded, repaired, recovery = persistence._load_startup_devices()

    assert recovery is False
    assert repaired is False
    assert len(loaded) == len(devices)
    assert _size(loaded) > config.MAX_CONFIG_BYTES


def test_cap_boundary_is_inclusive(monkeypatch) -> None:
    monkeypatch.setattr(runtime_state, "_devices_cache", _make_devices(1))
    devices = [persistence._norm(d) for d in _make_devices(1)]
    devices[0]["commands"] = _commands(1, 0)
    devices[0]["commands"][0]["command"] = "x" * (config.MAX_CONFIG_BYTES - _size(devices))
    assert _size(devices) == config.MAX_CONFIG_BYTES
    assert main._config_too_large(devices) is False
    devices[0]["commands"][0]["command"] += "x"
    assert main._config_too_large(devices) is True


def test_size_counts_escaped_non_ascii_text(monkeypatch) -> None:
    """Non-ASCII characters count at their escaped size, as sent on the socket."""
    monkeypatch.setattr(runtime_state, "_devices_cache", _make_devices(1))
    devices = [persistence._norm(d) for d in _make_devices(1)]
    devices[0]["commands"] = [{"name": "c", "command": "é" * 4000, "sudo": False, "confirm": ""}
                              for _ in range(50)]
    assert len(json.dumps(devices, ensure_ascii=False)) < config.MAX_CONFIG_BYTES
    assert _size(devices) > config.MAX_CONFIG_BYTES
    assert main._config_too_large(devices) is True


def test_startup_warns_when_saved_configuration_is_over_cap(monkeypatch, capsys) -> None:
    for name in ("_devices_cache", "_recovered_notice", "_recovery_mode", "_storage_warning"):
        monkeypatch.setattr(runtime_state, name, getattr(runtime_state, name))
    over = _over_cap_devices()
    monkeypatch.setattr(persistence, "_load_startup_devices", lambda: (over, None, False))

    async def _idle():
        return None

    monkeypatch.setattr(main, "_metrics_loop", _idle)
    monkeypatch.setattr(main, "_idle_loop", _idle)
    with TestClient(main.app, base_url="https://testserver"):
        pass
    out = capsys.readouterr().out
    assert "over the 1048576 byte limit" in out
    assert "x" * 100 not in out


def test_startup_is_quiet_when_saved_configuration_is_under_cap(monkeypatch, capsys) -> None:
    for name in ("_devices_cache", "_recovered_notice", "_recovery_mode", "_storage_warning"):
        monkeypatch.setattr(runtime_state, name, getattr(runtime_state, name))
    monkeypatch.setattr(persistence, "_load_startup_devices", lambda: (_make_devices(2), None, False))

    async def _idle():
        return None

    monkeypatch.setattr(main, "_metrics_loop", _idle)
    monkeypatch.setattr(main, "_idle_loop", _idle)
    with TestClient(main.app, base_url="https://testserver"):
        pass
    assert "byte limit" not in capsys.readouterr().out
