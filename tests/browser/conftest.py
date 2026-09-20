"""Fixtures for the browser (Playwright) tests.

These stand up a real dashboard over HTTPS on throwaway storage so screen
behavior can be checked without a manual smoke test. Everything here is scoped
to a single test: each test gets a fresh server process, a fresh temporary
private storage folder, and a fresh browser page.

The dashboard's session and CSRF cookies use the ``__Host-`` prefix and require
a secure origin, so the harness must serve HTTPS (a self-signed certificate the
browser is told to ignore) rather than plain HTTP.

The Playwright browser is downloaded separately (``playwright install
chromium``); when it is missing these tests skip cleanly so the rest of the
pytest suite still runs everywhere.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import expect

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = Path(__file__).with_name("_run_app.py")

# 15+ characters to satisfy the dashboard's password policy.
DASHBOARD_PASSWORD = "harness-password-123"

_SERVER_START_TIMEOUT = 30.0
_HOST = "127.0.0.1"


_BROWSER_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items) -> None:
    """Mark tests in this folder 'browser' so the default run skips them.

    This hook is global (pytest calls it with every collected item, not just
    this folder's), so it must filter by path or it would mark the whole suite.
    """
    for item in items:
        if _BROWSER_DIR in item.path.resolve().parents:
            item.add_marker("browser")


def _free_port() -> int:
    """Reserve a free TCP port, then release it for the server to claim."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((_HOST, 0))
        return probe.getsockname()[1]


def _write_self_signed_cert(cert_path: Path, key_path: Path) -> None:
    """Create a throwaway certificate valid for the loopback test host."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _HOST)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(__import__("ipaddress").ip_address(_HOST)), x509.DNSName("localhost")]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def _seed_auth_state(data_dir: Path) -> None:
    """Write a valid auth.json so the harness can log in."""
    sys.path.insert(0, str(REPO_ROOT))
    from auth import create_auth_state, publish_auth_state

    publish_auth_state(data_dir / "auth.json", create_auth_state(DASHBOARD_PASSWORD))


# A single seeded device for tests that need one already present (connect,
# host-key view, custom-command run, command-save). Mirrors the on-disk shape
# main.py writes: {"_app": ..., "devices": [...]} (see main.py's _save()).
SEEDED_DEVICE_ID = "dev_browsertest01"
SEEDED_DEVICE_HOST = "10.0.0.99"
SEEDED_DEVICE_NAME = "Seeded Test Device"


def _seed_devices(data_dir: Path) -> None:
    """Write a minimal devices.json with one device so tests skip Add Device."""
    payload = {
        "_app": "Simple Network Dashboard",
        "devices": [
            {
                "id": SEEDED_DEVICE_ID,
                "name": SEEDED_DEVICE_NAME,
                "host": SEEDED_DEVICE_HOST,
                "username": "tester",
                "metrics_port": 9100,
                "commands": [],
            }
        ],
    }
    (data_dir / "devices.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _wait_for_server(base_url: str, process: subprocess.Popen) -> None:
    """Poll the login page until the HTTPS server answers, or fail loudly."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    deadline = time.monotonic() + _SERVER_START_TIMEOUT
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"app server exited early with code {process.returncode}")
        try:
            with urllib.request.urlopen(f"{base_url}/login", timeout=2, context=context) as response:
                if response.status == 200:
                    return
        except Exception as error:  # noqa: BLE001 - retried until the deadline
            last_error = error
            time.sleep(0.25)
    raise RuntimeError(f"app server did not become ready within {_SERVER_START_TIMEOUT}s: {last_error}")


def _start_app_server(
    tmp_path: Path,
    *,
    with_device: bool = False,
    extra_env: dict[str, str] | None = None,
):
    """Shared body for the app_server fixtures: start uvicorn on fresh storage."""
    data_dir = tmp_path / "data"
    log_dir = tmp_path / "log"
    data_dir.mkdir()
    log_dir.mkdir()
    _seed_auth_state(data_dir)
    if with_device:
        _seed_devices(data_dir)

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    _write_self_signed_cert(cert_path, key_path)

    port = _free_port()
    base_url = f"https://{_HOST}:{port}"

    env = dict(os.environ)
    env.update(
        {
            "SND_DATA_DIR": str(data_dir),
            "SND_LOG_DIR": str(log_dir),
            "SND_PUBLIC_ORIGIN": base_url,
            "SND_TEST_HOST": _HOST,
            "SND_TEST_PORT": str(port),
            "SND_TEST_CERT": str(cert_path),
            "SND_TEST_KEY": str(key_path),
            "PYTHONPATH": str(REPO_ROOT),
        }
    )
    if extra_env:
        env.update(extra_env)
    process = subprocess.Popen(
        [sys.executable, str(LAUNCHER)],
        cwd=str(REPO_ROOT),
        env=env,
    )
    try:
        _wait_for_server(base_url, process)
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


@pytest.fixture
def app_server(tmp_path: Path) -> str:
    """Run the dashboard over HTTPS on fresh temp storage; yield its base URL."""
    yield from _start_app_server(tmp_path)


@pytest.fixture
def app_server_with_device(tmp_path: Path) -> str:
    """Like app_server, but devices.json is seeded with one device already."""
    yield from _start_app_server(tmp_path, with_device=True)


@pytest.fixture
def app_server_with_short_ws_revalidation(tmp_path: Path) -> str:
    """Run the dashboard with a short live WebSocket session recheck interval."""
    yield from _start_app_server(tmp_path, extra_env={"SND_WS_REVALIDATE_SECONDS": "1"})


@pytest.fixture(scope="session")
def _chromium_available(browser_type) -> None:
    """Skip the whole browser suite when Playwright's Chromium is not installed."""
    executable = browser_type.executable_path
    if not executable or not os.path.exists(executable):
        pytest.skip("Playwright Chromium is not installed (run: playwright install chromium)")


@pytest.fixture
def browser_context_args(browser_context_args: dict) -> dict:
    """Ignore the harness's self-signed certificate warning."""
    return {**browser_context_args, "ignore_https_errors": True}


@pytest.fixture
def logged_in_page(_chromium_available, page, app_server: str):
    """Return a Playwright page with a valid dashboard session."""
    page.goto(f"{app_server}/login")
    page.fill("#password", DASHBOARD_PASSWORD)
    page.click("#submit")
    page.wait_for_url(f"{app_server}/")
    # The version label is filled only when the WebSocket 'init' arrives, so
    # waiting for it proves both the login and the live socket succeeded. The
    # app's Content Security Policy forbids eval, so use a locator assertion
    # (DOM snapshot polling) rather than wait_for_function (page-side eval).
    expect(page.locator("#verLabel")).to_have_text(re.compile(r"^v"))
    return page


@pytest.fixture
def logged_in_page_with_device(_chromium_available, page, app_server_with_device: str):
    """Like logged_in_page, but one device card is already on the dashboard."""
    page.goto(f"{app_server_with_device}/login")
    page.fill("#password", DASHBOARD_PASSWORD)
    page.click("#submit")
    page.wait_for_url(f"{app_server_with_device}/")
    expect(page.locator("#verLabel")).to_have_text(re.compile(r"^v"))
    expect(page.locator(f'[data-card="{SEEDED_DEVICE_ID}"]')).to_be_visible()
    return page


@pytest.fixture
def connected_device_page(_chromium_available, page, app_server_with_device: str):
    """A logged-in page whose one device already looks SSH-connected.

    The seeded device's host is not a real SSH server, so a genuine connect
    would just fail. Instead this passes the real WebSocket traffic straight
    through to the live server (so login and the dashboard's own 'init' state
    still work normally), then injects one extra 'ssh_status: connected'
    frame on the client side only. That flips the same client-side state
    (SSH_STATE / the card's 'ssh-on' class) a real successful connect would,
    which is what reveals the custom-command input the recovery tests need to
    reach. The server never sees a real connection, so nothing there needs
    cleanup; the app_server fixture kills the process at teardown regardless.
    """
    ws_routes: list = []

    def handle_ws(ws) -> None:
        server = ws.connect_to_server()
        ws.on_message(lambda message: server.send(message))
        server.on_message(lambda message: ws.send(message))
        ws_routes.append(ws)

    page.route_web_socket(re.compile(r"/ws"), handle_ws)

    page.goto(f"{app_server_with_device}/login")
    page.fill("#password", DASHBOARD_PASSWORD)
    page.click("#submit")
    page.wait_for_url(f"{app_server_with_device}/")
    expect(page.locator("#verLabel")).to_have_text(re.compile(r"^v"))
    card = page.locator(f'[data-card="{SEEDED_DEVICE_ID}"]')
    expect(card).to_be_visible()

    assert ws_routes, "the dashboard's WebSocket never connected"
    ws_routes[0].send(json.dumps({
        "type": "ssh_status",
        "device_id": SEEDED_DEVICE_ID,
        "state": "connected",
    }))
    expect(card).to_have_class(re.compile(r"\bssh-on\b"))
    return page
