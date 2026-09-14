"""Task 4 Phase 3 HTTP authentication, CSRF, and browser-header contracts."""

from __future__ import annotations

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


def _configured_client(tmp_path: Path, monkeypatch) -> tuple[TestClient, SessionStore]:
    auth_file = tmp_path / "auth.json"
    auth.publish_auth_state(auth_file, auth.create_auth_state("a" * 15))
    store = SessionStore(tmp_path / "sessions.json")
    monkeypatch.setattr(main, "AUTH_FILE", str(auth_file))
    monkeypatch.setattr(main, "_session_store", store)
    monkeypatch.setattr(main, "_login_throttle", LoginThrottle())
    return TestClient(main.app, base_url="https://testserver"), store


def _authenticated_client(tmp_path: Path, monkeypatch) -> TestClient:
    client, _store = _configured_client(tmp_path, monkeypatch)
    assert client.get("/login").status_code == 200
    response = client.post(
        "/api/auth/login",
        json={"password": "a" * 15, "remembered": False},
        headers=_csrf_headers(client),
    )
    assert response.status_code == 200
    return client


def test_public_exceptions_do_not_require_a_session(tmp_path: Path, monkeypatch) -> None:
    client, _store = _configured_client(tmp_path, monkeypatch)

    assert client.get("/login").status_code == 200
    assert client.get("/api/auth/session").json() == {"authenticated": False}
    assert client.get("/static/favicon.svg").status_code == 200
    assert client.get("/certificate-setup").status_code == 200
    assert client.get("/certificate-setup/caddy-root-ca.crt").status_code == 404


def test_unauthenticated_root_redirects_and_private_api_returns_json_401(tmp_path: Path, monkeypatch) -> None:
    client, _store = _configured_client(tmp_path, monkeypatch)

    root = client.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/login"

    api = client.get("/api/devices")
    assert api.status_code == 401
    assert api.json() == {"detail": "Authentication required."}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_private_unsafe_methods_require_csrf_before_routing(tmp_path: Path, monkeypatch, method: str) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)

    response = client.request(method, "/api/devices")

    assert response.status_code == 403
    assert response.json() == {"detail": "CSRF validation failed."}


def test_login_requires_and_rotates_a_csrf_token(tmp_path: Path, monkeypatch) -> None:
    client, _store = _configured_client(tmp_path, monkeypatch)

    assert client.post("/api/auth/login", json={"password": "a" * 15}).status_code == 403
    client.get("/login")
    initial = client.cookies.get(main.CSRF_COOKIE_NAME)
    assert initial
    assert client.post(
        "/api/auth/login",
        json={"password": "a" * 15},
        headers={"X-CSRF-Token": "not-the-cookie"},
    ).status_code == 403

    accepted = client.post(
        "/api/auth/login",
        json={"password": "a" * 15},
        headers={"X-CSRF-Token": initial},
    )
    assert accepted.status_code == 200
    rotated = client.cookies.get(main.CSRF_COOKIE_NAME)
    assert rotated and rotated != initial
    cookie_headers = accepted.headers.get_list("set-cookie")
    assert any("__Host-snd-csrf=" in header and "HttpOnly" not in header for header in cookie_headers)
    assert any("Secure" in header and "SameSite=strict" in header and "Domain=" not in header for header in cookie_headers)


def test_remembered_session_restores_csrf_after_browser_restart(tmp_path: Path, monkeypatch) -> None:
    client, _store = _configured_client(tmp_path, monkeypatch)
    client.get("/login")
    login = client.post(
        "/api/auth/login",
        json={"password": "a" * 15, "remembered": True},
        headers=_csrf_headers(client),
    )
    assert login.status_code == 200
    session_token = client.cookies.get(main.SESSION_COOKIE_NAME)
    assert session_token

    restarted_client = TestClient(main.app, base_url="https://testserver")
    restarted_client.cookies.set(main.SESSION_COOKIE_NAME, session_token)

    dashboard = restarted_client.get("/")

    assert dashboard.status_code == 200
    assert restarted_client.cookies.get(main.CSRF_COOKIE_NAME)
    accepted = restarted_client.post("/api/ssh/disconnect_all", headers=_csrf_headers(restarted_client))
    assert accepted.status_code == 200


def test_logout_requires_csrf_and_clears_both_tokens_after_success(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    session_token = client.cookies.get(main.SESSION_COOKIE_NAME)

    rejected = client.post("/api/auth/logout")
    assert rejected.status_code == 403
    assert client.cookies.get(main.SESSION_COOKIE_NAME) == session_token

    logged_out = client.post("/api/auth/logout", headers=_csrf_headers(client))
    assert logged_out.status_code == 200
    cookie_headers = logged_out.headers.get_list("set-cookie")
    assert any("__Host-snd-session=" in header and "Max-Age=0" in header for header in cookie_headers)
    assert any("__Host-snd-csrf=" in header and "Max-Age=0" in header for header in cookie_headers)


def test_csrf_rejection_does_not_invoke_the_endpoint(tmp_path: Path, monkeypatch) -> None:
    client, _store = _configured_client(tmp_path, monkeypatch)
    calls = 0

    def disconnect_all():
        nonlocal calls
        calls += 1
        return {"ok": True}

    monkeypatch.setattr(main.ssh_mgr, "disconnect_all", disconnect_all)
    unauthenticated = client.post("/api/ssh/disconnect_all")
    assert unauthenticated.status_code == 401
    assert calls == 0

    client = _authenticated_client(tmp_path, monkeypatch)
    rejected = client.post("/api/ssh/disconnect_all")
    assert rejected.status_code == 403
    assert calls == 0

    accepted = client.post("/api/ssh/disconnect_all", headers=_csrf_headers(client))
    assert accepted.status_code == 200
    assert accepted.json() == {"ok": True}
    assert calls == 1


def test_security_headers_cover_success_error_and_static_responses(tmp_path: Path, monkeypatch) -> None:
    client, _store = _configured_client(tmp_path, monkeypatch)
    login = client.get("/login")
    denied = client.get("/api/devices")
    static = client.get("/static/favicon.svg")

    for response in (login, denied, static):
        assert response.headers["strict-transport-security"] == "max-age=31536000"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "object-src 'none'" in response.headers["content-security-policy"]
        assert "form-action 'self'" in response.headers["content-security-policy"]
        assert "connect-src 'self' wss://testserver" in response.headers["content-security-policy"]
        assert "connect-src 'self' wss:;" not in response.headers["content-security-policy"]
    assert login.headers["cache-control"] == "no-store"
    assert denied.headers["cache-control"] == "no-store"
    assert static.headers.get("cache-control") != "no-store"


def test_unhandled_error_response_keeps_security_headers(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)

    async def fail_unexpectedly() -> None:
        raise RuntimeError("private failure detail")

    main.app.add_api_route("/api/test-unhandled-error", fail_unexpectedly, methods=["GET"])
    temporary_route = main.app.routes[-1]
    try:
        with TestClient(main.app, base_url="https://testserver", raise_server_exceptions=False) as safe_client:
            safe_client.cookies.update(client.cookies)
            response = safe_client.get("/api/test-unhandled-error")
    finally:
        main.app.routes.remove(temporary_route)

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    assert "private failure detail" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
