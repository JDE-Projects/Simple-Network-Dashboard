"""Executable contracts for persistent Caddy certificate handling."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _find_bash() -> str | None:
    if os.name == "nt":
        candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
        if candidate.exists():
            return str(candidate)
    return shutil.which("bash")


BASH = _find_bash()


def _run_install(body: str) -> subprocess.CompletedProcess[str]:
    if BASH is None:
        pytest.skip("Bash is required to exercise certificate installer contracts")
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", f"source ./install.sh\n{body}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_standard_caddy_identity_and_persistent_data_home_are_required() -> None:
    result = _run_install(
        r'''
SERVICE_ENV=''
systemctl() {
    case "$*" in
        "show --property=User --value caddy") echo caddy ;;
        "show --property=Group --value caddy") echo caddy ;;
        "show --property=Environment --value caddy") echo "$SERVICE_ENV" ;;
        *) return 1 ;;
    esac
}
CADDY_HOME=/tmp
CADDY_DATA_HOME=/tmp/.local/share
getent() { echo 'caddy:x:999:999::/tmp:/usr/sbin/nologin'; }
verify_caddy_persistent_storage
SERVICE_ENV='XDG_DATA_HOME=/tmp/not-caddy'
if verify_caddy_persistent_storage; then exit 10; fi
SERVICE_ENV='XDG_DATA_HOME=/tmp/.local/share XDG_DATA_HOME=/tmp/not-caddy'
if verify_caddy_persistent_storage; then exit 11; fi
SERVICE_ENV='XDG_DATA_HOME=/tmp/not-caddy XDG_DATA_HOME=/tmp/.local/share'
if verify_caddy_persistent_storage; then exit 12; fi
'''
    )

    assert result.returncode == 0, result.stderr
    assert "non-standard XDG_DATA_HOME override" in result.stdout
    assert 'CADDY_HOME="/var/lib/caddy"' in (ROOT / "install.sh").read_text(encoding="utf-8")


def test_proxy_uses_internal_ca_without_leaf_lifetime_or_key_configuration() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")

    snippet = installer[installer.index("${CADDY_APP_MARKER}", installer.index("write_caddy_proxy_config")) :]
    assert "tls internal" in snippet
    assert "issuer" not in snippet
    assert "lifetime" not in snippet
    assert "key_type" not in snippet


def test_export_normalizes_only_the_public_root_and_prints_exact_file_checksum() -> None:
    result = _run_install(
        r'''
tmp=$(mktemp -d)
DATA_DIR="$tmp/data"
mkdir "$DATA_DIR"
CADDY_ROOT_CERT="$tmp/root.crt"
DASHBOARD_ROOT_CERT="$DATA_DIR/caddy-root-ca.crt"
printf 'PRIVATE KEY MATERIAL MUST NOT BE EXPORTED\nsource material\n' > "$CADDY_ROOT_CERT"
openssl() {
    if [[ "$*" == *"-ext basicConstraints"* ]]; then
        echo 'X509v3 Basic Constraints: critical'
        echo 'CA:TRUE'
    else
        printf '%s\n' '-----BEGIN CERTIFICATE-----' 'PUBLIC ROOT ONLY' '-----END CERTIFICATE-----' > "${@: -1}"
    fi
}
chown() { :; }
stat() { echo 'caddy:caddy:600'; }
export_caddy_root_certificate > "$tmp/export-output"
grep -Fxq 'PUBLIC ROOT ONLY' "$DASHBOARD_ROOT_CERT"
[ ! -e "$DASHBOARD_ROOT_CERT" ] || ! grep -q 'PRIVATE KEY' "$DASHBOARD_ROOT_CERT"
[ ! -e "$DATA_DIR/.caddy-root-ca.XXXXXX" ]
expected=$(sha256sum "$DASHBOARD_ROOT_CERT" | awk '{print $1}')
grep -Fqx "Exported Caddy root certificate file SHA-256 checksum: $expected" "$tmp/export-output"
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'chown snd:snd "$staged_cert"' in installer
    assert 'chmod 600 "$staged_cert"' in installer


@pytest.mark.parametrize("unsafe", ["destination", "source"])
def test_export_rejects_unsafe_certificate_paths_without_replacing_prior_export(unsafe: str) -> None:
    setup = (
        'ln -s "$tmp/other" "$DASHBOARD_ROOT_CERT" || exit 77\nprintf old-export > "$tmp/other"'
        if unsafe == "destination"
        else 'rm -f "$CADDY_ROOT_CERT"\nln -s "$tmp/other" "$CADDY_ROOT_CERT" || exit 77\nprintf source > "$tmp/other"'
    )
    result = _run_install(
        f'''
tmp=$(mktemp -d)
DATA_DIR="$tmp/data"
mkdir "$DATA_DIR"
CADDY_ROOT_CERT="$tmp/root.crt"
DASHBOARD_ROOT_CERT="$DATA_DIR/caddy-root-ca.crt"
printf source > "$CADDY_ROOT_CERT"
{setup}
stat() {{ echo 'caddy:caddy:600'; }}
openssl() {{ echo unexpected-openssl; return 1; }}
if export_caddy_root_certificate; then exit 10; fi
rm -rf "$tmp"
'''
    )

    if result.returncode == 77:
        pytest.skip("The current Windows test account cannot create symlinks")
    assert result.returncode == 0, result.stderr
    assert "unsafe" in result.stdout
    assert "unexpected-openssl" not in result.stdout


def test_certificate_export_failure_rolls_back_proxy_before_firewall() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")

    export_failure = installer.index("if ! export_caddy_root_certificate; then")
    rollback = installer.index("rollback_caddy_proxy_config", export_failure)
    firewall = installer.index("if ! configure_ufw_https; then")
    assert export_failure < rollback < firewall


def test_export_failure_preserves_an_existing_safe_public_export() -> None:
    result = _run_install(
        r'''
tmp=$(mktemp -d)
DATA_DIR="$tmp/data"
mkdir "$DATA_DIR"
CADDY_ROOT_CERT="$tmp/root.crt"
DASHBOARD_ROOT_CERT="$DATA_DIR/caddy-root-ca.crt"
printf source > "$CADDY_ROOT_CERT"
printf prior-public-export > "$DASHBOARD_ROOT_CERT"
stat() { echo 'caddy:caddy:600'; }
openssl() { return 1; }
if export_caddy_root_certificate; then exit 10; fi
grep -Fxq prior-public-export "$DASHBOARD_ROOT_CERT"
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "could not be validated" in result.stdout


def test_update_reexports_the_same_ca_without_changing_caddy_storage() -> None:
    result = _run_install(
        r'''
tmp=$(mktemp -d)
DATA_DIR="$tmp/data"
mkdir "$DATA_DIR"
CADDY_ROOT_CERT="$tmp/root.crt"
DASHBOARD_ROOT_CERT="$DATA_DIR/caddy-root-ca.crt"
printf persistent-caddy-ca > "$CADDY_ROOT_CERT"
cp "$CADDY_ROOT_CERT" "$tmp/original-root"
openssl() {
    if [[ "$*" == *"-ext basicConstraints"* ]]; then
        printf 'CA:TRUE\n'
    else
        printf '%s\n' '-----BEGIN CERTIFICATE-----' 'PUBLIC ROOT' '-----END CERTIFICATE-----' > "${@: -1}"
    fi
}
stat() { echo 'caddy:caddy:600'; }
chown() { :; }
export_caddy_root_certificate
printf stale-export > "$DASHBOARD_ROOT_CERT"
export_caddy_root_certificate
cmp "$CADDY_ROOT_CERT" "$tmp/original-root"
grep -Fxq 'PUBLIC ROOT' "$DASHBOARD_ROOT_CERT"
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("Exported Caddy root certificate file SHA-256 checksum:") == 2


def test_unsafe_root_certificate_metadata_preserves_an_existing_safe_export() -> None:
    result = _run_install(
        r'''
tmp=$(mktemp -d)
DATA_DIR="$tmp/data"
mkdir "$DATA_DIR"
CADDY_ROOT_CERT="$tmp/root.crt"
DASHBOARD_ROOT_CERT="$DATA_DIR/caddy-root-ca.crt"
printf source > "$CADDY_ROOT_CERT"
printf prior-public-export > "$DASHBOARD_ROOT_CERT"
stat() { echo 'root:root:644'; }
openssl() { echo unexpected-openssl; return 1; }
if export_caddy_root_certificate; then exit 10; fi
grep -Fxq prior-public-export "$DASHBOARD_ROOT_CERT"
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "unsafe ownership or permissions" in result.stdout
    assert "unexpected-openssl" not in result.stdout


def test_uninstall_preserves_caddy_account_and_uses_remove_not_purge() -> None:
    uninstaller = (ROOT / "uninstall.sh").read_text(encoding="utf-8")

    assert 'CADDY_HOME="/var/lib/caddy"' in uninstaller
    # A pre-existing or unknown-origin Caddy is preserved with this message.
    assert "Caddy package and persistent CA storage" in uninstaller
    # A dashboard-installed Caddy is removed with apt-get remove, never purge, so
    # the caddy service account is left in place.
    assert "apt-get remove -y caddy" in uninstaller
    assert "apt-get purge" not in uninstaller
