"""Task 4 Phase 2 browser-session and login contracts."""

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


def test_login_cookies_session_status_and_browser_id_non_authority(tmp_path: Path, monkeypatch) -> None:
    auth_file = tmp_path / "auth.json"
    auth.publish_auth_state(auth_file, auth.create_auth_state("a" * 15))
    store = SessionStore(tmp_path / "sessions.json")
    monkeypatch.setattr(main, "AUTH_FILE", str(auth_file))
    monkeypatch.setattr(main, "_session_store", store)
    monkeypatch.setattr(main, "_login_throttle", LoginThrottle())
    client = TestClient(main.app, base_url="https://testserver")

    rejected = client.post("/api/auth/login", json={"password": "wrong password", "remembered": False})
    assert rejected.status_code == 401
    response = client.post("/api/auth/login", json={"password": "a" * 15, "remembered": False})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "__Host-snd-session=" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert "Path=/" in cookie and "Domain=" not in cookie and "Max-Age" not in cookie
    assert client.get("/api/auth/session").json() == {"authenticated": True}

    remembered = client.post("/api/auth/login", json={"password": "a" * 15, "remembered": True})
    assert f"Max-Age={REMEMBERED_SECONDS}" in remembered.headers["set-cookie"]
    client.cookies.clear()
    assert client.get("/api/auth/session", headers={"X-Browser-Id": "attacker"}).json() == {"authenticated": False}


def test_logout_revokes_server_session_and_preserves_cookie_on_storage_failure(tmp_path: Path, monkeypatch) -> None:
    auth_file = tmp_path / "auth.json"
    auth.publish_auth_state(auth_file, auth.create_auth_state("a" * 15))
    store = SessionStore(tmp_path / "sessions.json")
    monkeypatch.setattr(main, "AUTH_FILE", str(auth_file))
    monkeypatch.setattr(main, "_session_store", store)
    client = TestClient(main.app, base_url="https://testserver")
    assert client.post("/api/auth/login", json={"password": "a" * 15, "remembered": False}).status_code == 200
    token = client.cookies.get(main.SESSION_COOKIE_NAME)
    logged_out = client.post("/api/auth/logout")
    assert logged_out.status_code == 200
    assert "Max-Age=0" in logged_out.headers["set-cookie"]
    assert not store.validate(token, auth.load_auth_state(auth_file)["session_generation"])

    token, _ = store.create(auth.load_auth_state(auth_file)["session_generation"], remembered=False)
    client.cookies.set(main.SESSION_COOKIE_NAME, token)
    monkeypatch.setattr(store, "revoke", lambda _token: (_ for _ in ()).throw(OSError("unavailable")))
    failed = client.post("/api/auth/logout")
    assert failed.status_code == 503
    assert "set-cookie" not in failed.headers
    assert client.cookies.get(main.SESSION_COOKIE_NAME) == token
    assert store.validate(token, auth.load_auth_state(auth_file)["session_generation"])


def test_login_failures_are_throttled_without_permanent_lockout(tmp_path: Path, monkeypatch) -> None:
    auth_file = tmp_path / "auth.json"
    auth.publish_auth_state(auth_file, auth.create_auth_state("a" * 15))
    now = [0.0]
    monkeypatch.setattr(main, "AUTH_FILE", str(auth_file))
    monkeypatch.setattr(main, "_session_store", SessionStore(tmp_path / "sessions.json"))
    monkeypatch.setattr(main, "_login_throttle", LoginThrottle(clock=lambda: now[0]))
    client = TestClient(main.app)
    for _ in range(5):
        response = client.post("/api/auth/login", json={"password": "wrong password", "remembered": False})
    assert response.status_code == 401 and response.json()["retry_after"] == 5
    assert client.post("/api/auth/login", json={"password": "a" * 15, "remembered": False}).status_code == 429
    now[0] += 15 * 60
    assert client.post("/api/auth/login", json={"password": "a" * 15, "remembered": False}).status_code == 200


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
        "/api/auth/login", json={"password": "a" * 15, "remembered": False}
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
