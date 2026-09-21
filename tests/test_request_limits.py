"""Request validation guardrails: unknown fields, oversized bodies, and the
app's own error shape for validation failures.

Phase 1 of request validation hardening adds three things to main.py:
strict request models that reject unexpected fields, a 1 MB request-body
limit enforced at the HTTP middleware level, and a reshaped validation-error
body that matches the app's ``{"ok": false, "error": ...}`` convention
instead of FastAPI's default ``{"detail": [...]}``.
"""

from __future__ import annotations

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
    monkeypatch.setattr(runtime_state, "_devices_cache", list(seed_devices))

    def _fake_save(devices: list) -> bool:
        runtime_state._devices_cache = list(devices)
        return True

    monkeypatch.setattr(persistence, "_save", _fake_save)

    async def _noop_broadcast(_message):
        return None

    monkeypatch.setattr(main.ws_mgr, "broadcast", _noop_broadcast)


def test_unknown_field_is_rejected_with_app_error_shape(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json={
            "name": "New Device", "host": "10.0.0.6", "username": "pi",
            "metrics_port": 9100, "unexpected_field": "nope",
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    body = response.json()
    assert body == {"ok": False, "error": "The request was not valid."}


def test_validation_error_does_not_use_fastapi_default_shape(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    # Missing a required field ("name") also goes through validation, and
    # must come back reshaped rather than as FastAPI's default {"detail": [...]}.
    response = client.post(
        "/api/devices",
        json={"host": "10.0.0.6", "username": "pi", "metrics_port": 9100},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    body = response.json()
    assert "detail" not in body
    assert body == {"ok": False, "error": "The request was not valid."}


def test_oversized_body_is_rejected(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    oversized_name = "x" * (main.MAX_REQUEST_BODY_BYTES + 1024)
    response = client.post(
        "/api/devices",
        json={"name": oversized_name, "host": "10.0.0.6", "username": "pi", "metrics_port": 9100},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 413
    assert response.json()["ok"] is False


def test_normal_valid_request_still_succeeds(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _stub_storage(monkeypatch, [])

    response = client.post(
        "/api/devices",
        json={"name": "New Device", "host": "10.0.0.6", "username": "pi", "metrics_port": 9100},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert len(body["devices"]) == 1
