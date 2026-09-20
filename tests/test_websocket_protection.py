"""WebSocket authentication and origin contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from starlette.websockets import WebSocket

import auth
import main
from session_manager import SessionStore


def _configured_client(tmp_path: Path, monkeypatch, origin: str = "https://dashboard.lan") -> TestClient:
    auth_file = tmp_path / "auth.json"
    auth.publish_auth_state(auth_file, auth.create_auth_state("a" * 15))
    monkeypatch.setattr(main, "AUTH_FILE", str(auth_file))
    monkeypatch.setattr(main, "_session_store", SessionStore(tmp_path / "sessions.json"))
    monkeypatch.setenv(main.PUBLIC_ORIGIN_ENV, origin)
    return TestClient(main.app, base_url=origin)


def _authenticated_client(tmp_path: Path, monkeypatch, origin: str = "https://dashboard.lan") -> TestClient:
    client = _configured_client(tmp_path, monkeypatch, origin)
    state = auth.load_auth_state(main.AUTH_FILE)
    token, _expiry = main._session_store.create(state["session_generation"], remembered=False)
    client.cookies.set(main.SESSION_COOKIE_NAME, token)
    return client


def _assert_policy_rejection(client: TestClient, headers=None, path: str = "/ws") -> None:
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(path, headers=headers or {}):
            pass
    assert rejected.value.code == main.WS_POLICY_VIOLATION_CODE


def test_ws_revalidate_seconds_defaults_to_ten_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("SND_WS_REVALIDATE_SECONDS", raising=False)

    assert main._ws_revalidate_seconds() == 10


def test_ws_revalidate_seconds_honors_valid_environment_value(monkeypatch) -> None:
    monkeypatch.setenv("SND_WS_REVALIDATE_SECONDS", "1.5")

    assert main._ws_revalidate_seconds() == 1.5


@pytest.mark.parametrize("value", ["", "garbage", "0", "-1"])
def test_ws_revalidate_seconds_falls_back_to_ten_for_invalid_values(monkeypatch, value: str) -> None:
    monkeypatch.setenv("SND_WS_REVALIDATE_SECONDS", value)

    assert main._ws_revalidate_seconds() == 10


def test_websocket_accepts_only_an_authenticated_browser_at_the_configured_origin(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)

    with client.websocket_connect("/ws?bid=browser-1", headers={"Origin": "https://dashboard.lan"}) as websocket:
        assert websocket.receive_json()["type"] == "init"


def test_websocket_rejects_missing_or_invalid_sessions(tmp_path: Path, monkeypatch) -> None:
    client = _configured_client(tmp_path, monkeypatch)
    _assert_policy_rejection(client, {"Origin": "https://dashboard.lan"})

    client.cookies.set(main.SESSION_COOKIE_NAME, "invalid")
    _assert_policy_rejection(client, {"Origin": "https://dashboard.lan"})


def test_websocket_rejection_has_no_connection_or_owner_side_effects(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    pending_releases = {}

    class RejectSideEffects:
        async def connect(self, *_args, **_kwargs) -> None:
            raise AssertionError("rejected WebSocket was accepted or registered")

        def drop(self, *_args, **_kwargs) -> None:
            raise AssertionError("rejected WebSocket changed connection state")

        def owner_count(self, *_args, **_kwargs) -> int:
            raise AssertionError("rejected WebSocket reached owner cleanup")

    def cancel_pending_release(*_args, **_kwargs) -> None:
        raise AssertionError("rejected WebSocket changed SSH release state")

    monkeypatch.setattr(main, "ws_mgr", RejectSideEffects())
    monkeypatch.setattr(main, "_cancel_pending_release", cancel_pending_release)
    monkeypatch.setattr(main, "_pending_releases", pending_releases)

    _assert_policy_rejection(
        client,
        {"Origin": "https://other.lan"},
        path="/ws?bid=rejected-browser",
    )
    assert pending_releases == {}


def test_websocket_rejects_expired_or_generation_invalidated_sessions(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    state = auth.load_auth_state(main.AUTH_FILE)
    token = client.cookies.get(main.SESSION_COOKIE_NAME)
    assert token
    main._session_store.revoke(token)
    _assert_policy_rejection(client, {"Origin": "https://dashboard.lan"})

    token, _expiry = main._session_store.create(state["session_generation"], remembered=False)
    client.cookies.set(main.SESSION_COOKIE_NAME, token)
    auth.publish_auth_state(main.AUTH_FILE, auth.create_auth_state("a" * 15))
    _assert_policy_rejection(client, {"Origin": "https://dashboard.lan"})


def test_live_websocket_closes_when_its_session_token_is_revoked(tmp_path: Path, monkeypatch) -> None:
    """A live socket closes after its own session token is revoked."""
    monkeypatch.setattr(main, "WS_REVALIDATE_SECONDS", 0.05)
    client = _authenticated_client(tmp_path, monkeypatch)
    token = client.cookies.get(main.SESSION_COOKIE_NAME)
    assert token

    with client.websocket_connect("/ws", headers={"Origin": "https://dashboard.lan"}) as websocket:
        assert websocket.receive_json()["type"] == "init"
        main._session_store.revoke(token)

        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                websocket.receive_json()
        assert closed.value.code == main.WS_POLICY_VIOLATION_CODE


def test_live_websocket_closes_when_session_generation_changes(tmp_path: Path, monkeypatch) -> None:
    """A live socket closes after all sessions are invalidated."""
    monkeypatch.setattr(main, "WS_REVALIDATE_SECONDS", 0.05)
    client = _authenticated_client(tmp_path, monkeypatch)

    with client.websocket_connect("/ws", headers={"Origin": "https://dashboard.lan"}) as websocket:
        assert websocket.receive_json()["type"] == "init"
        auth.publish_auth_state(main.AUTH_FILE, auth.create_auth_state("a" * 15))

        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                websocket.receive_json()
        assert closed.value.code == main.WS_POLICY_VIOLATION_CODE


@pytest.mark.parametrize(
    "headers",
    [
        None,
        {"Origin": "https://other.lan"},
        {"Origin": "http://dashboard.lan"},
        {"Origin": "https://DASHBOARD.lan"},
        {"Origin": "https://dashboard.lan:443"},
    ],
)
def test_websocket_rejects_missing_duplicate_and_mismatched_origins(tmp_path: Path, monkeypatch, headers) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)

    _assert_policy_rejection(client, headers)


@pytest.mark.parametrize(
    "origin_headers",
    [
        [(b"origin", b"https://dashboard.lan"), (b"origin", b"https://dashboard.lan")],
        [(b"origin", b"\xff")],
    ],
)
def test_websocket_rejects_duplicate_or_malformed_origin_headers(tmp_path: Path, monkeypatch, origin_headers) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    token = client.cookies.get(main.SESSION_COOKIE_NAME)
    assert token

    async def receive() -> dict:
        return {"type": "websocket.disconnect"}

    async def send(_message: dict) -> None:
        return None

    websocket = WebSocket(
        {
            "type": "websocket",
            "headers": origin_headers + [(b"cookie", f"{main.SESSION_COOKIE_NAME}={token}".encode())],
            "query_string": b"",
        },
        receive,
        send,
    )

    assert not asyncio.run(main._websocket_request_is_valid(websocket))


def test_websocket_uses_a_non_default_configured_origin(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch, "https://dashboard.lan:8443")

    with client.websocket_connect("/ws", headers={"Origin": "https://dashboard.lan:8443"}) as websocket:
        assert websocket.receive_json()["type"] == "init"
    _assert_policy_rejection(client, {"Origin": "https://dashboard.lan"})


def test_websocket_fails_closed_without_the_installed_public_origin(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    monkeypatch.delenv(main.PUBLIC_ORIGIN_ENV)

    _assert_policy_rejection(client, {"Origin": "https://dashboard.lan"})


def test_websocket_hides_authentication_storage_failures(tmp_path: Path, monkeypatch) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    monkeypatch.setattr(main._session_store, "validate", lambda *_args: (_ for _ in ()).throw(OSError("unavailable")))

    _assert_policy_rejection(client, {"Origin": "https://dashboard.lan"})


def test_installer_passes_the_canonical_public_origin_to_systemd() -> None:
    installer = (Path(__file__).parents[1] / "install.sh").read_text(encoding="utf-8")

    assert 'PUBLIC_ORIGIN="https://${HTTPS_HOST,,}"' in installer
    assert 'if [ "$HTTPS_PORT" -ne 443 ]; then' in installer
    assert 'PUBLIC_ORIGIN="${PUBLIC_ORIGIN}:${HTTPS_PORT}"' in installer
    assert "Environment=SND_PUBLIC_ORIGIN=${PUBLIC_ORIGIN}" in installer
