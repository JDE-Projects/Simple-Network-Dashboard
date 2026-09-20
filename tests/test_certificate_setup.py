"""Contracts for the local certificate setup page and public root download."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "static" / "certificate-setup.html"


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


def test_certificate_setup_route_serves_the_standalone_page(client: TestClient) -> None:
    response = client.get("/certificate-setup")
    assert response.status_code == 200
    assert response.content == PAGE.read_bytes()


def test_root_certificate_download_has_safe_attachment_headers(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    certificate = ROOT / "static" / "favicon.svg"
    monkeypatch.setattr(main, "DASHBOARD_ROOT_CERT", str(certificate))

    response = client.get("/certificate-setup/caddy-root-ca.crt")
    assert response.status_code == 200
    assert response.content == certificate.read_bytes()
    assert response.headers["content-disposition"] == 'attachment; filename="caddy-root-ca.crt"'
    assert response.headers["content-type"] == "application/x-x509-ca-cert"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("certificate", [ROOT / "missing-caddy-root-ca.crt", ROOT / "static"])
def test_root_certificate_download_rejects_missing_or_non_regular_files(client: TestClient, monkeypatch: pytest.MonkeyPatch, certificate: Path) -> None:
    monkeypatch.setattr(main, "DASHBOARD_ROOT_CERT", str(certificate))

    response = client.get("/certificate-setup/caddy-root-ca.crt")
    assert response.status_code == 404
    assert response.json() == {"detail": "The exported root certificate is not available yet."}
    assert str(certificate) not in response.text


def test_certificate_setup_page_is_local_themed_and_accessible() -> None:
    page = PAGE.read_text(encoding="utf-8")
    assert 'href="/static/favicon.svg?v=1"' in page
    assert 'url("/static/fonts/Sora-Regular.ttf")' in page
    assert 'url("/static/fonts/JetBrainsMono-Regular.ttf")' in page
    assert "localStorage.getItem('snd-theme')" in page
    assert "document.documentElement.classList.add('light')" in page
    assert "a:focus-visible,button:focus-visible" in page
    assert "@media (max-width:560px)" in page
    assert 'aria-label="Toggle theme"' in page
    assert 'alt="Simple Network Dashboard"' in page
    assert 'href="/certificate-setup/caddy-root-ca.crt"' in page
    assert 'download="caddy-root-ca.crt"' not in page
    assert "https://" not in page.replace("https://jde-projects.com", "")
    assert "http://" not in page


def test_certificate_page_orders_independent_verification_before_import() -> None:
    """The certificate page presents collapsed per-OS instructions after shared verification guidance."""
    page = PAGE.read_text(encoding="utf-8")
    checksum = page.index("independent SHA-256 checksum")
    windows = page.index("<summary>Windows</summary>")
    verify = page.index("Get-FileHash -Path")
    import_certificate = page.index("Import-Certificate -FilePath")
    assert checksum < windows
    assert verify < import_certificate
    assert "<summary>Windows</summary>" in page
    assert "<summary>Linux</summary>" in page
    assert "<summary>macOS</summary>" in page
    assert "<details open" not in page
    assert page.count("<details") == page.count("<details>")
    assert "We do not have a Mac to test" in page
    assert "Uninstalling the dashboard does not remove it" in page
    assert "Do not treat this page or its download as trusted until" in page


def test_installer_prints_certificate_setup_pointer_not_windows_commands() -> None:
    """The installer points every operating system to the certificate setup page."""
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    checksum = installer.index("Exported Caddy root certificate file SHA-256 checksum:")
    setup_url = installer.index("Certificate setup: https://${HTTPS_HOST}:${HTTPS_PORT}/certificate-setup")
    assert checksum < setup_url
    assert "step-by-step trust instructions for Windows, Linux, and macOS" in installer
    assert "Import-Certificate -FilePath" not in installer
    assert "Get-FileHash -Path" not in installer
