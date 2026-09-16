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
preflight_https
printf 'host=%s bind=%s port=%s backend=%s\\n' "$HTTPS_HOST" "$HTTPS_BIND" "$HTTPS_PORT" "${FORCED_PORT:-default}"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "host=172.20.1.9 bind=172.20.1.9 port=443 backend=default" in result.stdout


def test_defaults_choose_a_10_address_when_it_is_first_lan_address() -> None:
    result = _run_installer(
        """
MOCK_LAN_IPS='127.0.0.1 10.0.0.4 192.168.1.8'
parse_install_arguments
printf 'host=%s bind=%s\\n' "$HTTPS_HOST" "$HTTPS_BIND"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "host=10.0.0.4 bind=10.0.0.4" in result.stdout


def test_space_and_equals_flags_are_parsed() -> None:
    result = _run_installer(
        """
parse_install_arguments --port 3007 --https-host=dashboard.lan --https-port 8449
printf 'host=%s bind=%s port=%s backend=%s\\n' "$HTTPS_HOST" "$HTTPS_BIND" "$HTTPS_PORT" "$FORCED_PORT"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "host=dashboard.lan bind=10.0.0.4 port=8449 backend=3007" in result.stdout


def test_explicit_second_local_private_ip_becomes_the_https_host_and_bind_address() -> None:
    result = _run_installer(
        """
MOCK_LAN_IPS='10.0.0.4 192.168.1.8'
parse_install_arguments --https-host=192.168.1.8
printf 'host=%s bind=%s\\n' "$HTTPS_HOST" "$HTTPS_BIND"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "host=192.168.1.8 bind=192.168.1.8" in result.stdout


def test_private_but_nonlocal_https_host_is_rejected() -> None:
    result = _run_installer(
        """
if parse_install_arguments --https-host=192.168.1.8; then exit 10; fi
"""
    )

    assert result.returncode == 0, result.stderr
    assert "IPv4 addresses must be private and locally assigned" in result.stdout


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
        ("--https-host=0.0.0.0", "valid IPv4 address or DNS hostname"),
        ("--https-host=127.0.0.1", "valid IPv4 address or DNS hostname"),
        ("--https-host=8.8.8.8", "valid IPv4 address or DNS hostname"),
        ("--https-host=224.0.0.1", "valid IPv4 address or DNS hostname"),
        ("--https-host=255.255.255.255", "valid IPv4 address or DNS hostname"),
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


def test_https_host_requires_a_private_local_listener_address() -> None:
    result = _run_installer(
        """
MOCK_LAN_IPS='127.0.0.1 8.8.8.8'
if parse_install_arguments --https-host=dashboard.lan; then exit 10; fi
"""
    )

    assert result.returncode == 0, result.stderr
    assert "could not detect a private local IPv4 address" in result.stdout


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


def test_non_caddy_conflict_falls_back_to_next_free_https_port() -> None:
    result = _run_installer(
        """
MOCK_SS='LISTEN 0 128 *:443 *:* users:(("nginx",pid=9,fd=4))'
parse_install_arguments --https-host dashboard.lan --port 3007
preflight_https
printf 'rc=%s port=%s\\n' "$?" "$HTTPS_PORT"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "rc=0 port=8443" in result.stdout


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
    assert "no free HTTPS port found" in result.stdout


def test_explicit_https_port_is_honored_even_when_occupied() -> None:
    result = _run_installer(
        """
MOCK_SS='LISTEN 0 128 *:443 *:* users:(("nginx",pid=9,fd=4))'
parse_install_arguments --https-host 10.0.0.4 --https-port 443
preflight_https
printf 'rc=%s port=%s\\n' "$?" "$HTTPS_PORT"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "rc=0 port=443" in result.stdout


def test_fallback_walks_the_range_past_an_occupied_candidate() -> None:
    result = _run_installer(
        """
MOCK_SS='LISTEN 0 128 *:443 *:* users:(("nginx",pid=9,fd=4))'
MOCK_SS="$MOCK_SS"$'\\n''LISTEN 0 128 *:8443 *:* users:(("nginx",pid=9,fd=4))'
parse_install_arguments --https-host 10.0.0.4
preflight_https
printf 'rc=%s port=%s\\n' "$?" "$HTTPS_PORT"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "rc=0 port=8444" in result.stdout


def test_missing_ss_is_rejected() -> None:
    result = _run_installer(
        """
parse_install_arguments --https-host 10.0.0.4
unset -f ss
PATH=/not-a-real-command-directory
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


def test_free_ports_leave_redirects_enabled() -> None:
    result = _run_installer(
        """
parse_install_arguments --https-host 10.0.0.4
preflight_https
printf 'port=%s disable=%s\\n' "$HTTPS_PORT" "$HTTPS_DISABLE_REDIRECTS"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "port=443 disable=false" in result.stdout


def test_both_ports_occupied_falls_back_and_disables_redirects() -> None:
    result = _run_installer(
        """
MOCK_SS='LISTEN 0 128 *:443 *:* users:(("nginx",pid=9,fd=4))'
MOCK_SS="$MOCK_SS"$'\\n''LISTEN 0 128 *:80 *:* users:(("nginx",pid=9,fd=4))'
parse_install_arguments --https-host 10.0.0.4
preflight_https
printf 'port=%s disable=%s\\n' "$HTTPS_PORT" "$HTTPS_DISABLE_REDIRECTS"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "port=8443 disable=true" in result.stdout


def test_https_port_free_but_port_80_occupied_disables_redirects() -> None:
    result = _run_installer(
        """
MOCK_SS='LISTEN 0 128 *:80 *:* users:(("nginx",pid=9,fd=4))'
parse_install_arguments --https-host 10.0.0.4
preflight_https
printf 'port=%s disable=%s\\n' "$HTTPS_PORT" "$HTTPS_DISABLE_REDIRECTS"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "port=443 disable=true" in result.stdout


def test_port_80_held_only_by_our_own_caddy_leaves_redirects_enabled() -> None:
    result = _run_installer(
        """
caddy() { :; }
MOCK_LOAD_STATE=loaded
MOCK_EXEC_START='/usr/bin/caddy run --config /etc/caddy/Caddyfile'
MOCK_SS='LISTEN 0 128 *:80 *:* users:(("caddy",pid=42,fd=7))'
parse_install_arguments --https-host 10.0.0.4
preflight_https
printf 'disable=%s\\n' "$HTTPS_DISABLE_REDIRECTS"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "disable=false" in result.stdout


def test_caddy_global_block_state_detects_none_foreign_ours() -> None:
    result = _run_installer(
        """
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

printf 'example.com {\\n    reverse_proxy 127.0.0.1:3000\\n}\\n' > "$tmpdir/none.Caddyfile"
printf '{\\n    admin off\\n}\\n\\nexample.com {\\n}\\n' > "$tmpdir/foreign.Caddyfile"
printf '%s\\n{\\n\\tauto_https disable_redirects\\n}\\n\\nexample.com {\\n}\\n' "$CADDY_GLOBAL_MARKER" > "$tmpdir/ours.Caddyfile"
printf '# a comment\\n\\n# another comment\\nexample.com {\\n}\\n' > "$tmpdir/prefixed.Caddyfile"

printf 'none=%s foreign=%s ours=%s prefixed=%s\\n' \\
    "$(caddy_global_block_state "$tmpdir/none.Caddyfile")" \\
    "$(caddy_global_block_state "$tmpdir/foreign.Caddyfile")" \\
    "$(caddy_global_block_state "$tmpdir/ours.Caddyfile")" \\
    "$(caddy_global_block_state "$tmpdir/prefixed.Caddyfile")"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "none=none foreign=foreign ours=ours prefixed=none" in result.stdout


def test_reconcile_disable_true_prepends_owned_block_to_a_bare_file() -> None:
    result = _run_installer(
        """
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
printf 'example.com {\\n    reverse_proxy 127.0.0.1:3000\\n}\\n' > "$tmpdir/Caddyfile"

HTTPS_DISABLE_REDIRECTS=true
reconcile_caddy_global_block "$tmpdir/Caddyfile"
printf 'rc=%s\\n' "$?"
cat "$tmpdir/Caddyfile"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "rc=0" in result.stdout
    assert "# Simple Network Dashboard managed global options v1" in result.stdout
    assert "auto_https disable_redirects" in result.stdout
    assert "example.com {" in result.stdout
    assert "reverse_proxy 127.0.0.1:3000" in result.stdout


def test_reconcile_disable_true_refuses_a_foreign_block_and_leaves_it_unchanged() -> None:
    result = _run_installer(
        """
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
printf '{\\n    admin off\\n}\\n\\nexample.com {\\n}\\n' > "$tmpdir/Caddyfile"
before=$(cat "$tmpdir/Caddyfile")

HTTPS_DISABLE_REDIRECTS=true
if reconcile_caddy_global_block "$tmpdir/Caddyfile"; then exit 10; fi

after=$(cat "$tmpdir/Caddyfile")
if [ "$before" = "$after" ]; then
    echo "unchanged"
else
    echo "changed"
fi
"""
    )

    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "its own global options block" in combined
    assert "auto_https disable_redirects" in combined
    assert "unchanged" in result.stdout


@pytest.mark.parametrize("directive", ["auto_https disable_redirects", "auto_https off"])
def test_reconcile_disable_true_accepts_a_foreign_block_that_already_disables_redirects(
    directive: str,
) -> None:
    result = _run_installer(
        f"""
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
printf '{{\\n    {directive}\\n}}\\n\\nexample.com {{\\n}}\\n' > "$tmpdir/Caddyfile"
before=$(cat "$tmpdir/Caddyfile")

HTTPS_DISABLE_REDIRECTS=true
reconcile_caddy_global_block "$tmpdir/Caddyfile"

after=$(cat "$tmpdir/Caddyfile")
if [ "$before" = "$after" ]; then
    echo "unchanged"
else
    echo "changed"
fi
"""
    )

    assert result.returncode == 0, result.stderr
    assert "unchanged" in result.stdout


def test_reconcile_disable_true_leaves_an_already_owned_block_unchanged() -> None:
    result = _run_installer(
        """
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
printf '%s\\n{\\n\\tauto_https disable_redirects\\n}\\n\\nexample.com {\\n}\\n' "$CADDY_GLOBAL_MARKER" > "$tmpdir/Caddyfile"
before=$(cat "$tmpdir/Caddyfile")

HTTPS_DISABLE_REDIRECTS=true
reconcile_caddy_global_block "$tmpdir/Caddyfile"

after=$(cat "$tmpdir/Caddyfile")
if [ "$before" = "$after" ]; then
    echo "unchanged"
else
    echo "changed"
fi
"""
    )

    assert result.returncode == 0, result.stderr
    assert "unchanged" in result.stdout


def test_reconcile_disable_false_round_trips_an_owned_block_back_to_the_original() -> None:
    result = _run_installer(
        """
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
printf 'example.com {\\n    reverse_proxy 127.0.0.1:3000\\n}\\n' > "$tmpdir/Caddyfile"
original=$(cat "$tmpdir/Caddyfile")

HTTPS_DISABLE_REDIRECTS=true
reconcile_caddy_global_block "$tmpdir/Caddyfile"
HTTPS_DISABLE_REDIRECTS=false
reconcile_caddy_global_block "$tmpdir/Caddyfile"

final=$(cat "$tmpdir/Caddyfile")
if [ "$original" = "$final" ]; then
    echo "roundtrip-ok"
else
    echo "roundtrip-fail"
fi
"""
    )

    assert result.returncode == 0, result.stderr
    assert "roundtrip-ok" in result.stdout


def test_reconcile_disable_false_leaves_foreign_and_none_files_untouched() -> None:
    result = _run_installer(
        """
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
printf '{\\n    admin off\\n}\\n\\nexample.com {\\n}\\n' > "$tmpdir/foreign.Caddyfile"
printf 'example.com {\\n}\\n' > "$tmpdir/none.Caddyfile"
foreign_before=$(cat "$tmpdir/foreign.Caddyfile")
none_before=$(cat "$tmpdir/none.Caddyfile")

HTTPS_DISABLE_REDIRECTS=false
reconcile_caddy_global_block "$tmpdir/foreign.Caddyfile"
reconcile_caddy_global_block "$tmpdir/none.Caddyfile"

foreign_after=$(cat "$tmpdir/foreign.Caddyfile")
none_after=$(cat "$tmpdir/none.Caddyfile")
if [ "$foreign_before" = "$foreign_after" ] && [ "$none_before" = "$none_after" ]; then
    echo "unchanged"
else
    echo "changed"
fi
"""
    )

    assert result.returncode == 0, result.stderr
    assert "unchanged" in result.stdout


def test_https_preflight_precedes_the_first_mutating_install_step() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")

    preflight = installer.index("if ! preflight_https; then")
    assert preflight < installer.index("if ! classify_account_state; then")
    assert preflight < installer.index(
        'if [ "$ACCOUNT_STATE" = "fresh" ]; then\n    create_managed_snd_account'
    )
    assert preflight < installer.index('mkdir -p "$APP_DIR/static"')
    assert preflight < installer.index('systemctl stop "$SERVICE_NAME"')
