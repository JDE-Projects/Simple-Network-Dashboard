"""Malformed and wrong-typed request bodies, and the byte-count defense path
of the request-body size limit.

Phase 5 of request validation hardening does not add new behavior to
main.py; it fills verification gaps left by Phases 1-4's tests: a body that
is not JSON at all, fields sent with the wrong type, and the running
byte-count check in ``limit_request_body_size`` that catches an oversized
body even when the declared ``Content-Length`` header is missing or wrong.
"""

from __future__ import annotations

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
    monkeypatch.setattr(main, "_devices_cache", list(seed_devices))

    def _fake_save(devices: list) -> bool:
        main._devices_cache = list(devices)
        return True

    monkeypatch.setattr(main, "_save", _fake_save)

    async def _noop_broadcast(_message):
        return None

    monkeypatch.setattr(main.ws_mgr, "broadcast", _noop_broadcast)


def test_body_that_is_not_json_is_rejected_with_app_error_shape(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        content=b"{not valid json",
        headers={**_csrf_headers(client), "Content-Type": "application/json"},
    )

    assert response.status_code == 422
    body = response.json()
    assert "detail" not in body
    assert body == {"ok": False, "error": "The request was not valid."}


def test_metrics_port_as_non_numeric_string_is_rejected(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json={"name": "New Device", "host": "10.0.0.6", "username": "pi", "metrics_port": "abc"},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    body = response.json()
    assert "detail" not in body
    assert body == {"ok": False, "error": "The request was not valid."}


def test_commands_as_string_instead_of_list_is_rejected(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json={
            "name": "New Device", "host": "10.0.0.6", "username": "pi",
            "metrics_port": 9100, "commands": "not-a-list",
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    body = response.json()
    assert "detail" not in body
    assert body == {"ok": False, "error": "The request was not valid."}


def test_command_sudo_as_non_coercible_string_is_rejected(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json={
            "name": "New Device", "host": "10.0.0.6", "username": "pi",
            "metrics_port": 9100,
            "commands": [{"name": "c1", "command": "echo hi", "sudo": "maybe"}],
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    body = response.json()
    assert "detail" not in body
    assert body == {"ok": False, "error": "The request was not valid."}


def test_oversized_body_with_missing_content_length_is_rejected_by_byte_count(
    tmp_path: Path, monkeypatch
) -> None:
    """Forces a chunked request (no Content-Length header at all) so the
    declared-length check in ``limit_request_body_size`` cannot fire, and
    only the running byte-count check reads the oversized body and rejects
    it.
    """
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    oversized_name = "x" * (main.MAX_REQUEST_BODY_BYTES + 1024)
    payload = (
        b'{"name": "' + oversized_name.encode() + b'", "host": "10.0.0.6", '
        b'"username": "pi", "metrics_port": 9100}'
    )

    def _chunks():
        chunk_size = 65536
        for start in range(0, len(payload), chunk_size):
            yield payload[start:start + chunk_size]

    headers = {**_csrf_headers(client), "Content-Type": "application/json"}
    request = client.build_request("POST", "/api/devices", content=_chunks(), headers=headers)
    assert "content-length" not in {key.lower() for key in request.headers.keys()}

    response = client.send(request)

    assert response.status_code == 413
    body = response.json()
    assert body["ok"] is False
