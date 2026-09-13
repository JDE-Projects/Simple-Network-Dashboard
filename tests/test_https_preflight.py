"""Executable contracts for the installer HTTPS and Caddy preflight."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _find_bash() -> str | None:
    if os.name == "nt":
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        if git_bash.exists():
            return str(git_bash)
    return shutil.which("bash")


BASH = _find_bash()


def _run_installer(body: str) -> subprocess.CompletedProcess[str]:
    if BASH is None:
        pytest.skip("Bash is required to exercise installer HTTPS preflight contracts")
    script = f"""
source ./install.sh
MOCK_LAN_IPS='10.0.0.4 127.0.0.1'
MOCK_LOAD_STATE=not-found
MOCK_EXEC_START=''
MOCK_SS=''
hostname() {{ printf '%s\\n' "$MOCK_LAN_IPS"; }}
systemctl() {{
    if [ "$1" = show ] && [ "$2" = --property=LoadState ]; then
        printf '%s\\n' "$MOCK_LOAD_STATE"
        return 0
    fi
    if [ "$1" = show ] && [ "$2" = --property=ExecStart ]; then
        printf '%s\\n' "$MOCK_EXEC_START"
        return 0
    fi
    return 1
}}
ss() {{ printf '%s\\n' "$MOCK_SS"; }}
{body}
"""
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_defaults_choose_first_lan_address_and_https_port() -> None:
    result = _run_installer(
        """
MOCK_LAN_IPS='127.0.0.1 172.20.1.9 10.0.0.4'
parse_install_arguments
printf 'host=%s port=%s backend=%s\\n' "$HTTPS_HOST" "$HTTPS_PORT" "${FORCED_PORT:-default}"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "host=172.20.1.9 port=443 backend=default" in result.stdout


def test_defaults_choose_a_10_address_when_it_is_first_lan_address() -> None:
    result = _run_installer(
        """
MOCK_LAN_IPS='127.0.0.1 10.0.0.4 192.168.1.8'
parse_install_arguments
printf 'host=%s\\n' "$HTTPS_HOST"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "host=10.0.0.4" in result.stdout


def test_space_and_equals_flags_are_parsed() -> None:
    result = _run_installer(
        """
parse_install_arguments --port 3007 --https-host=dashboard.lan --https-port 8449
printf 'host=%s port=%s backend=%s\\n' "$HTTPS_HOST" "$HTTPS_PORT" "$FORCED_PORT"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "host=dashboard.lan port=8449 backend=3007" in result.stdout


@pytest.mark.parametrize(
    "arguments, expected",
    [
        ("--https-port", "requires a value"),
        ("--https-port=abc", "must be a numeric port"),
        ("--https-port=0", "must be a numeric port"),
        ("--https-port=65536", "must be a numeric port"),
        ("--https-host='bad host'", "valid IPv4 address or DNS hostname"),
        ("--https-host=-bad.lan", "valid IPv4 address or DNS hostname"),
        ("--https-host=999.1.1.1", "valid IPv4 address or DNS hostname"),
        ("--https-host=256.255.255.255", "valid IPv4 address or DNS hostname"),
    ],
)
def test_invalid_https_arguments_are_rejected(arguments: str, expected: str) -> None:
    result = _run_installer(
        f"""
if parse_install_arguments {arguments}; then exit 10; fi
"""
    )

    assert result.returncode == 0, result.stderr
    assert expected in result.stdout


def test_ipv4_boundary_value_is_accepted_as_an_explicit_https_host() -> None:
    result = _run_installer(
        """
parse_install_arguments --https-host=255.255.255.255
printf 'host=%s\\n' "$HTTPS_HOST"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "host=255.255.255.255" in result.stdout


def test_fresh_state_with_free_port_is_accepted() -> None:
    result = _run_installer(
        """
parse_install_arguments --https-host 10.0.0.4
preflight_https
"""
    )

    assert result.returncode == 0, result.stderr


def test_compatible_caddy_is_accepted_when_port_is_free_or_caddy_owned() -> None:
    result = _run_installer(
        """
caddy() { :; }
MOCK_LOAD_STATE=loaded
MOCK_EXEC_START='/usr/bin/caddy run --environ --config /etc/caddy/Caddyfile'
parse_install_arguments --https-host 10.0.0.4
preflight_https
MOCK_SS='LISTEN 0 4096 *:443 *:* users:(("caddy",pid=42,fd=7))'
preflight_https
"""
    )

    assert result.returncode == 0, result.stderr


def test_non_caddy_conflict_prints_verified_alternative_command() -> None:
    result = _run_installer(
        """
MOCK_SS='LISTEN 0 128 *:443 *:* users:(("nginx",pid=9,fd=4))'
parse_install_arguments --https-host dashboard.lan --port 3007
if preflight_https; then exit 10; fi
"""
    )

    assert result.returncode == 0, result.stderr
    assert "*:443" in result.stdout
    assert (
        "sudo bash install.sh --https-host dashboard.lan --https-port 8443 --port 3007"
        in result.stdout
    )


def test_conflict_reports_when_no_alternative_port_is_free() -> None:
    result = _run_installer(
        """
MOCK_SS='LISTEN 0 128 *:443 *:* users:(("nginx",pid=9,fd=4))'
for port in $(seq 8443 8453); do
    MOCK_SS="$MOCK_SS"$'\\n'"LISTEN 0 128 *:$port *:* users:((\"other\",pid=9,fd=4))"
done
parse_install_arguments --https-host 10.0.0.4
if preflight_https; then exit 10; fi
"""
    )

    assert result.returncode == 0, result.stderr
    assert "No free alternative HTTPS port was found in range 8443-8453." in result.stdout


def test_missing_ss_is_rejected() -> None:
    result = _run_installer(
        """
unset -f ss
PATH=/not-a-real-command-directory
parse_install_arguments --https-host 10.0.0.4
if preflight_https; then exit 10; fi
"""
    )

    assert result.returncode == 0, result.stderr
    assert "ss is required" in result.stdout


@pytest.mark.parametrize(
    "load_state, exec_start, with_binary, expected",
    [
        ("not-found", "", True, "binary found"),
        ("loaded", "/usr/bin/caddy run --resume", True, "API/resume"),
        ("loaded", "/usr/bin/caddy run --config /etc/caddy/caddy.json", True, "API/resume"),
        ("loaded", "/usr/bin/caddy start --config /etc/caddy/Caddyfile", True, "not launched"),
        ("loaded", "/usr/bin/caddy run --config /etc/caddy/site.conf", True, "does not use a Caddyfile"),
        ("loaded", "/usr/bin/caddy run --config /etc/caddy/Caddyfile", False, "binary could not be found"),
    ],
)
def test_unsupported_caddy_states_are_rejected(
    load_state: str, exec_start: str, with_binary: bool, expected: str
) -> None:
    binary = "caddy() { :; }" if with_binary else ""
    result = _run_installer(
        f"""
{binary}
MOCK_LOAD_STATE='{load_state}'
MOCK_EXEC_START='{exec_start}'
parse_install_arguments --https-host 10.0.0.4
if preflight_https; then exit 10; fi
"""
    )

    assert result.returncode == 0, result.stderr
    assert expected in result.stdout


def test_https_preflight_precedes_the_first_mutating_install_step() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")

    preflight = installer.index("if ! preflight_https; then")
    assert preflight < installer.index("if ! classify_account_state; then")
    assert preflight < installer.index(
        'if [ "$ACCOUNT_STATE" = "fresh" ]; then\n    create_managed_snd_account'
    )
    assert preflight < installer.index('mkdir -p "$APP_DIR/static"')
    assert preflight < installer.index('systemctl stop "$SERVICE_NAME"')
