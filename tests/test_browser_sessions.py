"""Browser-session and login contracts."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from argon2.exceptions import HashingError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

import auth
import main
from session_manager import LoginThrottle, REMEMBERED_SECONDS, SESSION_ONLY_SECONDS, SessionStorageError, SessionStore


def _csrf_headers(client: TestClient) -> dict[str, str]:
    client.get("/login")
    token = client.cookies.get(main.CSRF_COOKIE_NAME)
    assert token
    return {"X-CSRF-Token": token}


def test_session_storage_never_persists_the_plaintext_token(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json", clock=lambda: 1_000)
    token, expiry = store.create("generation", remembered=False)
    contents = (tmp_path / "sessions.json").read_text(encoding="utf-8")
    assert token not in contents
    assert expiry == 1_000 + SESSION_ONLY_SECONDS
    assert store.validate(token, "generation")
    if os.name != "nt":
        assert (tmp_path / "sessions.json").stat().st_mode & 0o777 == 0o600


def test_remembered_and_session_only_expiries_are_exact(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json", clock=lambda: 1_000)
    _, session_expiry = store.create("generation", remembered=False)
    _, remembered_expiry = store.create("generation", remembered=True)
    assert session_expiry == 1_000 + SESSION_ONLY_SECONDS
    assert remembered_expiry == 1_000 + REMEMBERED_SECONDS


def test_expired_sessions_are_pruned(tmp_path: Path) -> None:
    now = [1_000.0]
    store = SessionStore(tmp_path / "sessions.json", clock=lambda: now[0])
    expired, _ = store.create("generation", remembered=False)
    now[0] += SESSION_ONLY_SECONDS
    assert not store.validate(expired, "generation")
    assert json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))["sessions"] == {}


def test_password_reset_generation_invalidates_and_prunes_sessions(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json", clock=lambda: 1_000)
    token, _ = store.create("before-reset", remembered=False)
    assert not store.validate(token, "after-reset")
    assert json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))["sessions"] == {}


def test_logout_rejects_replay(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    token, _ = store.create("generation", remembered=False)
    store.revoke(token)
    assert not store.validate(token, "generation")


def test_unsafe_or_malformed_session_storage_is_rejected(tmp_path: Path) -> None:
    destination = tmp_path / "sessions.json"
    destination.write_text('{"version":1,"sessions":{"bad":{}}}', encoding="utf-8")
    store = SessionStore(destination)
    try:
        store.validate("token", "generation")
    except SessionStorageError:
        pass
    else:
        raise AssertionError("malformed private session storage was accepted")


@pytest.mark.skipif(os.name == "nt", reason="POSIX link safety behavior is unavailable on Windows")
@pytest.mark.parametrize("link_kind", ["symbolic", "hard"])
def test_linked_session_storage_is_rejected(tmp_path: Path, link_kind: str) -> None:
    target = tmp_path / "target"
    target.write_text('{"version":1,"sessions":{}}', encoding="utf-8")
    destination = tmp_path / "sessions.json"
    if link_kind == "symbolic":
        os.symlink(target, destination)
    else:
        os.link(target, destination)
    with pytest.raises(SessionStorageError):
        SessionStore(destination).validate("token", "generation")


def test_concurrent_session_creates_retain_every_live_record(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    start = threading.Barrier(8)

    def create() -> str:
        start.wait()
        return store.create("generation", remembered=False)[0]

    with ThreadPoolExecutor(max_workers=8) as executor:
        tokens = list(executor.map(lambda _value: create(), range(8)))
    stored = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))["sessions"]
    assert len(stored) == 8
    assert all(store.validate(token, "generation") for token in tokens)


def test_progressive_login_throttle_and_recovery() -> None:
    now = [0.0]
    throttle = LoginThrottle(clock=lambda: now[0])
    for _ in range(4):
        assert throttle.failure("client") == 0
    assert throttle.failure("client") == 5
    assert throttle.retry_after("client") == 5
    now[0] += 5
    assert throttle.failure("client") == 10
    throttle.success("client")
    assert throttle.retry_after("client") == 0
    for _ in range(5):
        throttle.failure("client")
    now[0] += 15 * 60
    assert throttle.retry_after("client") == 0


def test_login_cookies_and_session_status(tmp_path: Path, monkeypatch) -> None:
    auth_file = tmp_path / "auth.json"
    auth.publish_auth_state(auth_file, auth.create_auth_state("a" * 15))
    store = SessionStore(tmp_path / "sessions.json")
    monkeypatch.setattr(main, "AUTH_FILE", str(auth_file))
    monkeypatch.setattr(main, "_session_store", store)
    monkeypatch.setattr(main, "_login_throttle", LoginThrottle())
    client = TestClient(main.app, base_url="https://testserver")

    rejected = client.post("/api/auth/login", json={"password": "wrong password", "remembered": False}, headers=_csrf_headers(client))
    assert rejected.status_code == 401
    response = client.post("/api/auth/login", json={"password": "a" * 15, "remembered": False}, headers=_csrf_headers(client))
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "__Host-snd-session=" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert "Path=/" in cookie and "Domain=" not in cookie and "Max-Age" not in cookie
    assert client.get("/api/auth/session").json() == {"authenticated": True}

    remembered = client.post("/api/auth/login", json={"password": "a" * 15, "remembered": True}, headers=_csrf_headers(client))
    assert f"Max-Age={REMEMBERED_SECONDS}" in remembered.headers["set-cookie"]
    client.cookies.clear()
    assert client.get("/api/auth/session").json() == {"authenticated": False}


def test_logout_revokes_server_session_and_preserves_cookie_on_storage_failure(tmp_path: Path, monkeypatch) -> None:
    auth_file = tmp_path / "auth.json"
    auth.publish_auth_state(auth_file, auth.create_auth_state("a" * 15))
    store = SessionStore(tmp_path / "sessions.json")
    monkeypatch.setattr(main, "AUTH_FILE", str(auth_file))
    monkeypatch.setattr(main, "_session_store", store)
    client = TestClient(main.app, base_url="https://testserver")
    assert client.post("/api/auth/login", json={"password": "a" * 15, "remembered": False}, headers=_csrf_headers(client)).status_code == 200
    token = client.cookies.get(main.SESSION_COOKIE_NAME)
    logged_out = client.post("/api/auth/logout", headers=_csrf_headers(client))
    assert logged_out.status_code == 200
    assert "Max-Age=0" in logged_out.headers["set-cookie"]
    assert not store.validate(token, auth.load_auth_state(auth_file)["session_generation"])

    token, _ = store.create(auth.load_auth_state(auth_file)["session_generation"], remembered=False)
    client.cookies.set(main.SESSION_COOKIE_NAME, token)
    monkeypatch.setattr(store, "revoke", lambda _token: (_ for _ in ()).throw(OSError("unavailable")))
    failed = client.post("/api/auth/logout", headers=_csrf_headers(client))
    assert failed.status_code == 503
    assert "set-cookie" not in failed.headers
    assert client.cookies.get(main.SESSION_COOKIE_NAME) == token
    assert store.validate(token, auth.load_auth_state(auth_file)["session_generation"])


def test_logout_releases_only_its_session_owner_immediately(tmp_path: Path, monkeypatch) -> None:
    auth_file = tmp_path / "auth.json"
    auth.publish_auth_state(auth_file, auth.create_auth_state("a" * 15))
    store = SessionStore(tmp_path / "sessions.json")
    monkeypatch.setattr(main, "AUTH_FILE", str(auth_file))
    monkeypatch.setattr(main, "_session_store", store)
    monkeypatch.setattr(main, "_pending_releases", {})
    client = TestClient(main.app, base_url="https://testserver")
    assert client.post("/api/auth/login", json={"password": "a" * 15, "remembered": False}, headers=_csrf_headers(client)).status_code == 200
    signed_out_token = client.cookies.get(main.SESSION_COOKIE_NAME)
    assert signed_out_token
    other_token, _expiry = store.create(auth.load_auth_state(auth_file)["session_generation"], remembered=False)
    owners_with_sessions = {
        main.SessionStore.token_digest(signed_out_token),
        main.SessionStore.token_digest(other_token),
    }
    released: list[str] = []

    def release_owner(owner: str) -> None:
        released.append(owner)
        owners_with_sessions.remove(owner)

    monkeypatch.setattr(main.ssh_mgr, "release_owner", release_owner)

    response = client.post("/api/auth/logout", headers=_csrf_headers(client))

    signed_out_owner = main.SessionStore.token_digest(signed_out_token)
    assert response.status_code == 200
    assert released == [signed_out_owner]
    assert owners_with_sessions == {main.SessionStore.token_digest(other_token)}


def test_logout_logs_ssh_release_failure_but_still_completes(tmp_path: Path, monkeypatch, capsys) -> None:
    auth_file = tmp_path / "auth.json"
    auth.publish_auth_state(auth_file, auth.create_auth_state("a" * 15))
    store = SessionStore(tmp_path / "sessions.json")
    monkeypatch.setattr(main, "AUTH_FILE", str(auth_file))
    monkeypatch.setattr(main, "_session_store", store)
    client = TestClient(main.app, base_url="https://testserver")
    assert client.post("/api/auth/login", json={"password": "a" * 15, "remembered": False}, headers=_csrf_headers(client)).status_code == 200
    debug_lines: list[str] = []
    monkeypatch.setattr(main.ssh_mgr, "release_owner", lambda _owner: (_ for _ in ()).throw(RuntimeError("close failed")))
    monkeypatch.setattr(main.debug_log, "_debug_write", debug_lines.append)

    response = client.post("/api/auth/logout", headers=_csrf_headers(client))

    assert response.status_code == 200
    expected = "SIGN-OUT SSH RELEASE FAILED: RuntimeError: close failed"
    assert debug_lines == [expected]
    assert expected in capsys.readouterr().out


def test_ssh_routes_use_the_signed_in_session_owner_and_ignore_legacy_header(tmp_path: Path, monkeypatch) -> None:
    auth_file = tmp_path / "auth.json"
    auth.publish_auth_state(auth_file, auth.create_auth_state("a" * 15))
    store = SessionStore(tmp_path / "sessions.json")
    monkeypatch.setattr(main, "AUTH_FILE", str(auth_file))
    monkeypatch.setattr(main, "_session_store", store)
    monkeypatch.setattr(main.runtime_state, "_devices_cache", [{"id": "device-1", "host": "host"}])
    first = TestClient(main.app, base_url="https://testserver")
    second = TestClient(main.app, base_url="https://testserver")
    generation = auth.load_auth_state(auth_file)["session_generation"]
    first_token, _expiry = store.create(generation, remembered=False)
    second_token, _expiry = store.create(generation, remembered=False)
    first.cookies.set(main.SESSION_COOKIE_NAME, first_token)
    second.cookies.set(main.SESSION_COOKIE_NAME, second_token)
    first_owner = main.SessionStore.token_digest(first_token)
    second_owner = main.SessionStore.token_digest(second_token)
    session_owner = {"device-1": first_owner}
    observed: list[str] = []

    def protect(owner: str) -> dict[str, object]:
        observed.append(owner)
        if owner != session_owner["device-1"]:
            return {"ok": False, "not_owner": True}
        return {"ok": True}

    monkeypatch.setattr(main.ssh_mgr, "connect", lambda _id, _password, _device, owner: protect(owner))
    monkeypatch.setattr(main.ssh_mgr, "disconnect", lambda _id, owner: protect(owner))
    monkeypatch.setattr(main.ssh_mgr, "run_command", lambda _id, _cmd, _sudo, _label, owner, cmd_id: protect(owner))
    monkeypatch.setattr(main.ssh_mgr, "cancel", lambda _id, owner: protect(owner))
    monkeypatch.setattr(main.ssh_mgr, "trust_host_key", lambda _id, _code, owner: protect(owner))
    monkeypatch.setattr(main.ssh_mgr, "reject_host_key", lambda _id, _code, owner: protect(owner))

    legacy_headers = {"X-Browser-Id": "attacker-chosen-owner", **_csrf_headers(second)}
    responses = [
        second.post("/api/ssh/connect", json={"device_id": "device-1", "password": "pw"}, headers=legacy_headers),
        second.post("/api/ssh/disconnect", json={"device_id": "device-1"}, headers=legacy_headers),
        second.post("/api/ssh/run", json={"device_id": "device-1", "command": "id"}, headers=legacy_headers),
        second.post("/api/ssh/cancel", json={"device_id": "device-1"}, headers=legacy_headers),
        second.post("/api/ssh/trust_key", json={"device_id": "device-1", "code": "code"}, headers=legacy_headers),
        second.post("/api/ssh/reject_key", json={"device_id": "device-1", "code": "code"}, headers=legacy_headers),
    ]

    assert all(response.json() == {"ok": False, "not_owner": True} for response in responses)
    assert observed == [second_owner] * len(responses)
    assert first_owner not in observed


def test_login_failures_are_throttled_without_permanent_lockout(tmp_path: Path, monkeypatch) -> None:
    auth_file = tmp_path / "auth.json"
    auth.publish_auth_state(auth_file, auth.create_auth_state("a" * 15))
    now = [0.0]
    monkeypatch.setattr(main, "AUTH_FILE", str(auth_file))
    monkeypatch.setattr(main, "_session_store", SessionStore(tmp_path / "sessions.json"))
    monkeypatch.setattr(main, "_login_throttle", LoginThrottle(clock=lambda: now[0]))
    client = TestClient(main.app, base_url="https://testserver")
    for _ in range(5):
        response = client.post("/api/auth/login", json={"password": "wrong password", "remembered": False}, headers=_csrf_headers(client))
    assert response.status_code == 401 and response.json()["retry_after"] == 5
    assert client.post("/api/auth/login", json={"password": "a" * 15, "remembered": False}, headers=_csrf_headers(client)).status_code == 429
    now[0] += 15 * 60
    assert client.post("/api/auth/login", json={"password": "a" * 15, "remembered": False}, headers=_csrf_headers(client)).status_code == 200


def test_argon2_verification_failure_returns_sanitized_unavailable_response(
    tmp_path: Path, monkeypatch
) -> None:
    auth_file = tmp_path / "auth.json"
    auth.publish_auth_state(auth_file, auth.create_auth_state("a" * 15))
    monkeypatch.setattr(main, "AUTH_FILE", str(auth_file))
    monkeypatch.setattr(main, "_login_throttle", LoginThrottle())
    monkeypatch.setattr(
        main,
        "verify_password",
        lambda _state, _password: (_ for _ in ()).throw(HashingError("argon2 failed")),
    )
    client = TestClient(main.app, base_url="https://testserver")

    response = client.post(
        "/api/auth/login", json={"password": "a" * 15, "remembered": False}, headers=_csrf_headers(client)
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Dashboard authentication is unavailable."}
    assert "set-cookie" not in response.headers


def test_concurrent_login_burst_serializes_password_verification(monkeypatch) -> None:
    calls = 0
    state = {"session_generation": "generation"}
    monkeypatch.setattr(main, "load_auth_state", lambda _path: state)
    monkeypatch.setattr(main, "_login_throttle", LoginThrottle())
    monkeypatch.setattr(main, "_login_lock", asyncio.Lock())

    def verify(_state: dict[str, object], _password: str) -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(main, "verify_password", verify)

    def request() -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "scheme": "https",
                "path": "/api/auth/login",
                "headers": [],
                "client": ("127.0.0.1", 1234),
                "server": ("testserver", 443),
                "query_string": b"",
            }
        )

    async def burst() -> tuple[list[JSONResponse], JSONResponse]:
        responses = await asyncio.gather(
            *(main.login(main.LoginIn(password="bad"), request()) for _ in range(6))
        )
        next_response = await main.login(main.LoginIn(password="bad"), request())
        return responses, next_response

    responses, next_response = asyncio.run(burst())
    assert calls == 5
    assert [response.status_code for response in responses].count(401) == 5
    assert [response.status_code for response in responses].count(429) == 1
    assert next_response.status_code == 429
