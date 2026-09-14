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
    assert 'aria-label="Switch to light theme"' in page
    assert 'alt="Simple Network Dashboard"' in page
    assert 'href="/certificate-setup/caddy-root-ca.crt"' in page
    assert 'download="caddy-root-ca.crt"' not in page
    assert "https://" not in page.replace("https://jde-projects.com", "")
    assert "http://" not in page


def test_certificate_page_orders_independent_verification_before_import() -> None:
    page = PAGE.read_text(encoding="utf-8")
    checksum = page.index("installer printed its independent SHA-256 checksum")
    verify = page.index("Get-FileHash -Path")
    import_certificate = page.index("Import-Certificate -FilePath")
    assert checksum < verify < import_certificate
    assert "Do not treat this page or its download as trusted until" in page


def test_installer_prints_certificate_setup_and_ordered_windows_guidance() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    checksum = installer.index("Exported Caddy root certificate file SHA-256 checksum:")
    setup_url = installer.index("Certificate setup: https://${HTTPS_HOST}:${HTTPS_PORT}/certificate-setup")
    verify = installer.index('Get-FileHash -Path "$env:USERPROFILE\\Downloads\\caddy-root-ca.crt" -Algorithm SHA256')
    import_certificate = installer.index('Import-Certificate -FilePath "$env:USERPROFILE\\Downloads\\caddy-root-ca.crt" -CertStoreLocation Cert:\\CurrentUser\\Root')
    assert checksum < setup_url < verify < import_certificate
