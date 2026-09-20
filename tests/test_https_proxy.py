"""Executable contracts for the Caddy proxy and firewall integration."""

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
        pytest.skip("Bash is required to exercise proxy installer contracts")
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", f"source ./install.sh\n{body}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_uninstall(body: str) -> subprocess.CompletedProcess[str]:
    if BASH is None:
        pytest.skip("Bash is required to exercise proxy uninstall contracts")
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", f"source ./uninstall.sh\n{body}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_backend_port_selection_covers_auto_override_update_and_exhaustion() -> None:
    result = _run_install(
        r'''
tmp=$(mktemp -d)
UNIT_DEST="$tmp/service"
ss() { printf 'LISTEN 0 128 127.0.0.1:3000 0.0.0.0:*\nLISTEN 0 128 127.0.0.1:3001 0.0.0.0:*\n'; }
FORCED_PORT=""
select_backend_port
printf 'auto=%s\n' "$PORT"
FORCED_PORT=4123
select_backend_port
printf 'forced=%s\n' "$PORT"
FORCED_PORT=""
printf 'ExecStart=/app/main.py --port 3008\n' > "$UNIT_DEST"
select_backend_port
printf 'update=%s\n' "$PORT"
rm -f "$UNIT_DEST"
FORCED_PORT=""
ss() { for port in $(seq 3000 3010); do printf 'LISTEN 0 128 127.0.0.1:%s 0.0.0.0:*\n' "$port"; done; }
if select_backend_port; then exit 10; fi
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "auto=3002" in result.stdout
    assert "forced=4123" in result.stdout
    assert "update=3008" in result.stdout
    assert "no free port found in range 3000-3010" in result.stdout


def test_proxy_config_preserves_caddyfile_and_validates_before_publishing() -> None:
    result = _run_install(
        r'''
tmp=$(mktemp -d)
CADDY_CONFIG_PATH="$tmp/Caddyfile"
CADDY_APP_CONFIG="$tmp/simple-network-dashboard.caddy"
CADDY_IMPORT_LINE="import $CADDY_APP_CONFIG"
CADDY_BINARY=caddy
HTTPS_HOST=dashboard.lan
HTTPS_BIND=10.0.0.4
HTTPS_PORT=8443
PORT=3007
printf ':80 { respond "shared" }\n' > "$CADDY_CONFIG_PATH"
caddy() { echo validated; }
systemctl() {
    [ "$1" = is-active ] && return 0
    [ "$1" = reload ] && echo reloaded
}
write_caddy_proxy_config
cat "$CADDY_CONFIG_PATH"
cat "$CADDY_APP_CONFIG"
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    assert ':80 { respond "shared" }' in result.stdout
    assert "import " in result.stdout
    assert "dashboard.lan:8443" in result.stdout
    assert "bind " not in result.stdout
    assert "reverse_proxy 127.0.0.1:3007" in result.stdout
    assert result.stdout.index("validated") < result.stdout.index("reloaded")


def test_reload_or_start_caddy_reloads_a_running_caddy() -> None:
    result = _run_install(
        r'''
systemctl() {
    if [ "$1" = is-active ]; then return 0; fi
    printf 'systemctl %s\n' "$*"
}
reload_or_start_caddy
'''
    )

    assert result.returncode == 0, result.stderr
    assert "systemctl reload caddy" in result.stdout
    assert "systemctl start caddy" not in result.stdout


def test_reload_or_start_caddy_starts_a_stopped_caddy() -> None:
    result = _run_install(
        r'''
systemctl() {
    if [ "$1" = is-active ]; then return 1; fi
    printf 'systemctl %s\n' "$*"
}
reload_or_start_caddy
'''
    )

    assert result.returncode == 0, result.stderr
    assert "systemctl start caddy" in result.stdout
    assert "systemctl reload caddy" not in result.stdout


def test_proxy_publication_starts_caddy_instead_of_reloading_when_not_active() -> None:
    result = _run_install(
        r'''
tmp=$(mktemp -d)
CADDY_CONFIG_PATH="$tmp/Caddyfile"
CADDY_APP_CONFIG="$tmp/simple-network-dashboard.caddy"
CADDY_IMPORT_LINE="import $CADDY_APP_CONFIG"
CADDY_BINARY=caddy
HTTPS_HOST=dashboard.lan
HTTPS_BIND=10.0.0.4
HTTPS_PORT=8443
PORT=3007
printf ':80 { respond "shared" }\n' > "$CADDY_CONFIG_PATH"
caddy() { echo validated; }
started=false
systemctl() {
    if [ "$1" = is-active ]; then
        [ "$started" = true ] && return 0
        return 1
    fi
    if [ "$1" = start ]; then started=true; fi
    printf 'systemctl %s\n' "$*"
}
write_caddy_proxy_config
cat "$CADDY_CONFIG_PATH"
cat "$CADDY_APP_CONFIG"
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "dashboard.lan:8443" in result.stdout
    assert "reverse_proxy 127.0.0.1:3007" in result.stdout
    assert "systemctl start caddy" in result.stdout
    assert "systemctl reload caddy" not in result.stdout


def test_ufw_active_replaces_only_labelled_rules_with_https_rule() -> None:
    result = _run_install(
        r'''
HTTPS_PORT=8443
PORT=3007
ufw() {
    if [ "$1" = status ] && [ "${2:-}" = numbered ]; then
        printf '[ 2] 3000/tcp ALLOW IN Anywhere # Simple Network Dashboard HTTPS\n'
        printf '[ 1] 22/tcp ALLOW IN Anywhere\n'
    elif [ "$1" = status ]; then
        printf 'Status: active\n'
    else
        printf 'ufw %s\n' "$*"
    fi
}
configure_ufw_https
'''
    )

    assert result.returncode == 0, result.stderr
    assert "ufw --force delete 2" in result.stdout
    assert "ufw allow 8443/tcp comment Simple Network Dashboard HTTPS" in result.stdout
    assert result.stdout.index("ufw allow") < result.stdout.index("ufw --force delete")
    assert "3007/tcp" not in result.stdout


def test_ufw_replacement_deletes_only_stale_rules_after_allow_renumbers_them() -> None:
    result = _run_install(
        r'''
HTTPS_PORT=8443
rule_state=before
deleted=""
ufw() {
    if [ "$1" = status ] && [ "${2:-}" = numbered ]; then
        if [ "$rule_state" = before ]; then
            printf '[ 4] 443/tcp ALLOW IN Anywhere (v6) # Simple Network Dashboard HTTPS\n'
            printf '[ 3] 443/tcp ALLOW IN Anywhere # Simple Network Dashboard HTTPS\n'
            printf '[ 2] 22/tcp ALLOW IN Anywhere\n'
        else
            printf '[ 6] 8443/tcp ALLOW IN Anywhere (v6) # Simple Network Dashboard HTTPS\n'
            printf '[ 5] 8443/tcp ALLOW IN Anywhere # Simple Network Dashboard HTTPS\n'
            printf '[ 4] 443/tcp ALLOW IN Anywhere (v6) # Simple Network Dashboard HTTPS\n'
            printf '[ 3] 443/tcp ALLOW IN Anywhere # Simple Network Dashboard HTTPS\n'
            printf '[ 2] 22/tcp ALLOW IN Anywhere\n'
        fi
    elif [ "$1" = status ]; then
        printf 'Status: active\n'
    elif [ "$1" = allow ]; then
        rule_state=after
        printf 'allowed %s\n' "$*"
    elif [ "$1" = --force ] && [ "$2" = delete ]; then
        case "$3" in
            4|3) deleted="${deleted}${3} " ;;
            *) echo "unexpected-delete-$3"; return 1 ;;
        esac
    fi
}
configure_ufw_https
printf 'deleted=%s\n' "$deleted"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "allowed allow 8443/tcp comment Simple Network Dashboard HTTPS" in result.stdout
    assert result.stdout.index("allowed allow") < result.stdout.index("deleted=4 3")
    assert "deleted=4 3" in result.stdout
    assert "unexpected-delete" not in result.stdout


def test_ufw_inactive_is_not_changed() -> None:
    result = _run_install(
        r'''
HTTPS_PORT=8443
ufw() { printf 'Status: inactive\n'; }
configure_ufw_https
'''
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_ufw_allow_failure_preserves_old_rules_and_caddy_is_rolled_back() -> None:
    result = _run_install(
        r'''
tmp=$(mktemp -d)
CADDY_CONFIG_PATH="$tmp/Caddyfile"
CADDY_APP_CONFIG="$tmp/simple-network-dashboard.caddy"
CADDY_ROLLBACK_CADDYFILE="$tmp/old-Caddyfile"
CADDY_ROLLBACK_APP_CONFIG="$tmp/old-app.caddy"
CADDY_ROLLBACK_HAD_APP_CONFIG=true
printf 'new caddy\n' > "$CADDY_CONFIG_PATH"
printf 'new app\n' > "$CADDY_APP_CONFIG"
printf 'old caddy\n' > "$CADDY_ROLLBACK_CADDYFILE"
printf 'old app\n' > "$CADDY_ROLLBACK_APP_CONFIG"
HTTPS_PORT=8443
ufw() {
    if [ "$1" = status ] && [ "${2:-}" = numbered ]; then
        printf '[ 2] 443/tcp ALLOW IN Anywhere # Simple Network Dashboard HTTPS\n'
    elif [ "$1" = status ]; then
        printf 'Status: active\n'
    elif [ "$1" = allow ]; then
        echo allow-failed
        return 1
    else
        echo unexpected-delete
    fi
}
systemctl() { echo caddy-reloaded; }
if configure_ufw_https; then exit 10; fi
rollback_caddy_proxy_config
grep -Fxq 'old caddy' "$CADDY_CONFIG_PATH"
grep -Fxq 'old app' "$CADDY_APP_CONFIG"
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "allow-failed" in result.stdout
    assert "existing dashboard rules were preserved" in result.stdout
    assert "unexpected-delete" not in result.stdout
    assert "caddy-reloaded" in result.stdout

    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    failure = installer.index("if ! configure_ufw_https; then")
    assert installer.index("rollback_caddy_proxy_config", failure) > failure


def test_phase_two_uses_official_caddy_repository_and_https_output() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "dl.cloudsmith.io/public/caddy/stable/gpg.key" in installer
    assert "dl.cloudsmith.io/public/caddy/stable/debian.deb.txt" in installer
    assert "/usr/share/keyrings/caddy-stable-archive-keyring.gpg" in installer
    assert "/etc/apt/sources.list.d/caddy-stable.list" in installer
    assert "apt-get install -y caddy" in installer
    assert 'echo "Open https://${HTTPS_HOST}:${HTTPS_PORT} in your browser."' in installer
    assert "reverse_proxy 127.0.0.1:${PORT}" in installer
    assert "tls internal" in installer


def test_fresh_caddy_install_uses_prerequisites_and_reinspects() -> None:
    result = _run_install(
        r'''
CADDY_STATE=fresh
apt-get() { printf 'apt %s\n' "$*"; }
curl() { printf 'curl %s\n' "$*"; }
gpg() { cat >/dev/null; }
chmod() { :; }
install() { :; }
inspect_caddy_state() { CADDY_STATE=compatible; CADDY_BINARY=caddy; }
install_caddy_if_fresh
'''
    )

    assert result.returncode == 0, result.stderr
    assert "apt install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg" in result.stdout
    assert "stable/debian.deb.txt" in result.stdout
    assert "apt install -y caddy" in result.stdout


def test_fresh_caddy_install_rejects_incompatible_post_install_state() -> None:
    result = _run_install(
        r'''
CADDY_STATE=fresh
apt-get() { :; }
curl() { : > "${@: -1}"; }
gpg() { :; }
install() { :; }
chmod() { :; }
inspect_caddy_state() { CADDY_STATE=fresh; }
if install_caddy_if_fresh; then exit 10; fi
'''
    )

    assert result.returncode == 0, result.stderr
    assert "not a compatible Caddyfile-managed systemd service" in result.stdout


def test_fresh_caddy_install_stops_when_official_repository_download_fails() -> None:
    result = _run_install(
        r'''
CADDY_STATE=fresh
apt-get() { printf 'apt %s\n' "$*"; }
curl() { echo download-failed; return 1; }
gpg() { echo unexpected-gpg; }
if install_caddy_if_fresh; then exit 10; fi
'''
    )

    assert result.returncode == 0, result.stderr
    assert "download-failed" in result.stdout
    assert "unexpected-gpg" not in result.stdout
    assert "apt install -y caddy" not in result.stdout


def test_fresh_caddy_install_replaces_stock_welcome_site() -> None:
    result = _run_install(
        r'''
tmp=$(mktemp -d)
CADDY_STATE=fresh
apt-get() { :; }
curl() { : > "${@: -1}"; }
gpg() { :; }
chmod() { :; }
printf ':80 {\n\troot * /usr/share/caddy\n\tfile_server\n}\n' > "$tmp/Caddyfile"
install() {
    if [ "$5" = "$tmp/Caddyfile" ]; then cp -- "$4" "$5"; fi
}
inspect_caddy_state() { CADDY_STATE=compatible; CADDY_BINARY=caddy; CADDY_CONFIG_PATH="$tmp/Caddyfile"; }
install_caddy_if_fresh
cat "$tmp/Caddyfile"
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "file_server" not in result.stdout
    assert ":80" not in result.stdout


@pytest.mark.parametrize("kind", ["unowned", "symlink"])
def test_existing_unowned_or_symlink_proxy_config_is_refused(kind: str) -> None:
    setup = (
        'printf "not ours\\n" > "$CADDY_APP_CONFIG"\n'
        'stat() { printf "1000:1000:644\\n"; }'
        if kind == "unowned"
        else 'ln -s target "$CADDY_APP_CONFIG" || exit 77'
    )
    result = _run_install(
        f'''
tmp=$(mktemp -d)
CADDY_CONFIG_PATH="$tmp/Caddyfile"
CADDY_APP_CONFIG="$tmp/simple-network-dashboard.caddy"
CADDY_IMPORT_LINE="import $CADDY_APP_CONFIG"
printf ':80 {{ respond "shared" }}\\n' > "$CADDY_CONFIG_PATH"
{setup}
if write_caddy_proxy_config; then exit 10; fi
rm -rf "$tmp"
'''
    )

    if result.returncode == 77:
        pytest.skip("The current Windows test account cannot create symlinks")
    assert result.returncode == 0, result.stderr
    assert "not a root-owned marked regular file" in result.stdout


def test_bare_dashboard_import_is_refused_instead_of_adopted() -> None:
    result = _run_install(
        r'''
tmp=$(mktemp -d)
CADDY_CONFIG_PATH="$tmp/Caddyfile"
CADDY_APP_CONFIG="$tmp/simple-network-dashboard.caddy"
CADDY_IMPORT_LINE="import $CADDY_APP_CONFIG"
printf '%s\n' "$CADDY_IMPORT_LINE" > "$CADDY_CONFIG_PATH"
if write_caddy_proxy_config; then exit 10; fi
cat "$CADDY_CONFIG_PATH"
[ ! -e "$CADDY_APP_CONFIG" ]
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "unowned or duplicate dashboard import" in result.stdout


def test_validation_happens_before_caddy_configuration_publication() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")

    validation = installer.index('validate --config "$validation_caddyfile" --adapter caddyfile')
    publication = installer.index('install -m 0644 -- "$staged_app_config" "$CADDY_APP_CONFIG"')
    reload = installer.index("reload_or_start_caddy", publication)
    assert validation < publication < reload


def test_backend_must_start_before_proxy_and_firewall_are_attempted() -> None:
    result = _run_install(
        r'''
systemctl() { printf 'systemctl %s\n' "$*"; return 1; }
write_caddy_proxy_config() { echo proxy-published; }
configure_ufw_https() { echo firewall-opened; }
if start_dashboard_backend && write_caddy_proxy_config && configure_ufw_https; then exit 10; fi
'''
    )

    assert result.returncode == 0, result.stderr
    assert "systemctl start simple-network-dashboard" in result.stdout
    assert "proxy-published" not in result.stdout
    assert "firewall-opened" not in result.stdout

    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert installer.index("if ! start_dashboard_backend; then") < installer.index(
        "if ! write_caddy_proxy_config; then"
    ) < installer.index("if ! configure_ufw_https; then")


@pytest.mark.parametrize("failed_publication", [1, 2])
def test_proxy_publication_failure_restores_prior_state(failed_publication: int) -> None:
    result = _run_install(
        rf'''
tmp=$(mktemp -d)
CADDY_CONFIG_PATH="$tmp/Caddyfile"
CADDY_APP_CONFIG="$tmp/simple-network-dashboard.caddy"
CADDY_IMPORT_LINE="import $CADDY_APP_CONFIG"
CADDY_BINARY=caddy
HTTPS_HOST=dashboard.lan
HTTPS_BIND=10.0.0.4
HTTPS_PORT=443
PORT=3000
printf ':80 {{ respond "shared" }}\n' > "$CADDY_CONFIG_PATH"
cp "$CADDY_CONFIG_PATH" "$tmp/original"
caddy() {{ :; }}
systemctl() {{ echo unexpected-reload; return 0; }}
publish_count=0
install() {{
    publish_count=$((publish_count + 1))
    if [ "$publish_count" -eq {failed_publication} ]; then return 1; fi
    cp "${{@: -2:1}}" "${{@: -1}}"
}}
if write_caddy_proxy_config; then exit 10; fi
cmp "$CADDY_CONFIG_PATH" "$tmp/original"
[ ! -e "$CADDY_APP_CONFIG" ]
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "publication failed" in result.stdout
    assert "unexpected-reload" not in result.stdout


def test_proxy_reload_failure_restores_existing_empty_owned_snippet() -> None:
    result = _run_install(
        r'''
tmp=$(mktemp -d)
CADDY_CONFIG_PATH="$tmp/Caddyfile"
CADDY_APP_CONFIG="$tmp/simple-network-dashboard.caddy"
CADDY_IMPORT_LINE="import $CADDY_APP_CONFIG"
CADDY_BINARY=caddy
HTTPS_HOST=dashboard.lan
HTTPS_BIND=10.0.0.4
HTTPS_PORT=443
PORT=3000
printf ':80 { respond "shared" }\n' > "$CADDY_CONFIG_PATH"
printf '%s\n' "$CADDY_APP_MARKER" > "$CADDY_APP_CONFIG"
cp "$CADDY_CONFIG_PATH" "$tmp/original-caddy"
cp "$CADDY_APP_CONFIG" "$tmp/original-app"
stat() { printf '0:0:644\n'; }
caddy() { :; }
reload_count=0
systemctl() {
    [ "$1" = is-active ] && return 0
    reload_count=$((reload_count + 1))
    [ "$reload_count" -gt 1 ]
}
if write_caddy_proxy_config; then exit 10; fi
cmp "$CADDY_CONFIG_PATH" "$tmp/original-caddy"
cmp "$CADDY_APP_CONFIG" "$tmp/original-app"
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "reload failed" in result.stdout


def test_proxy_publication_restores_state_when_caddy_does_not_become_active() -> None:
    result = _run_install(
        r'''
tmp=$(mktemp -d)
CADDY_CONFIG_PATH="$tmp/Caddyfile"
CADDY_APP_CONFIG="$tmp/simple-network-dashboard.caddy"
CADDY_IMPORT_LINE="import $CADDY_APP_CONFIG"
CADDY_BINARY=caddy
HTTPS_HOST=dashboard.lan
HTTPS_BIND=10.0.0.4
HTTPS_PORT=443
PORT=3000
printf ':80 { respond "shared" }\n' > "$CADDY_CONFIG_PATH"
cp "$CADDY_CONFIG_PATH" "$tmp/original-caddy"
caddy() { :; }
is_active_count=0
systemctl() {
    if [ "$1" = is-active ]; then
        is_active_count=$((is_active_count + 1))
        [ "$is_active_count" -eq 1 ]
        return
    fi
    return 0
}
if write_caddy_proxy_config; then exit 10; fi
cmp "$CADDY_CONFIG_PATH" "$tmp/original-caddy"
[ ! -e "$CADDY_APP_CONFIG" ]
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "did not come up" in result.stdout


@pytest.mark.parametrize("failed_copy", [1, 2])
def test_proxy_staging_or_backup_copy_failure_stops_before_publication(
    failed_copy: int,
) -> None:
    result = _run_install(
        rf'''
tmp=$(mktemp -d)
CADDY_CONFIG_PATH="$tmp/Caddyfile"
CADDY_APP_CONFIG="$tmp/simple-network-dashboard.caddy"
CADDY_IMPORT_LINE="import $CADDY_APP_CONFIG"
CADDY_BINARY=caddy
HTTPS_HOST=dashboard.lan
HTTPS_BIND=10.0.0.4
HTTPS_PORT=443
PORT=3000
printf ':80 {{ respond "shared" }}\n' > "$CADDY_CONFIG_PATH"
copy_count=0
cp() {{
    copy_count=$((copy_count + 1))
    if [ "$copy_count" -eq {failed_copy} ]; then return 1; fi
    command cp "$@"
}}
caddy() {{ :; }}
install() {{ echo unexpected-publication; return 0; }}
systemctl() {{ echo unexpected-reload; return 0; }}
if write_caddy_proxy_config; then exit 10; fi
[ ! -e "$CADDY_APP_CONFIG" ]
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "unexpected-publication" not in result.stdout
    assert "unexpected-reload" not in result.stdout


def test_proxy_restoration_failure_is_reported() -> None:
    result = _run_install(
        r'''
tmp=$(mktemp -d)
CADDY_CONFIG_PATH="$tmp/Caddyfile"
CADDY_APP_CONFIG="$tmp/simple-network-dashboard.caddy"
CADDY_IMPORT_LINE="import $CADDY_APP_CONFIG"
CADDY_BINARY=caddy
HTTPS_HOST=dashboard.lan
HTTPS_BIND=10.0.0.4
HTTPS_PORT=443
PORT=3000
printf ':80 { respond "shared" }\n' > "$CADDY_CONFIG_PATH"
caddy() { :; }
publish_count=0
install() {
    publish_count=$((publish_count + 1))
    [ "$publish_count" -eq 1 ] && command cp "${@: -2:1}" "${@: -1}" && return 0
    return 1
}
if write_caddy_proxy_config; then exit 10; fi
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "previous Caddyfile could not be restored" in result.stdout


def test_uninstall_removes_only_owned_caddy_and_ufw_integration() -> None:
    uninstaller = (ROOT / "uninstall.sh").read_text(encoding="utf-8")

    assert 'CADDY_APP_CONFIG="/etc/caddy/simple-network-dashboard.caddy"' in uninstaller
    assert 'CADDY_IMPORT_LINE="import ${CADDY_APP_CONFIG}"' in uninstaller
    assert 'UFW_COMMENT="Simple Network Dashboard HTTPS"' in uninstaller
    assert 'awk -v comment="$CADDY_IMPORT_COMMENT" -v import_line="$CADDY_IMPORT_LINE"' in uninstaller
    assert 'validate --config "$staged_caddyfile" --adapter caddyfile' in uninstaller
    # The dashboard-owned proxy import is removed here; a dashboard-installed
    # Caddy package is removed with apt-get remove, never purge (which would also
    # delete the caddy service account).
    assert "apt-get purge" not in uninstaller
    assert 'CADDY_HOME="/var/lib/caddy"' in uninstaller


def test_uninstall_executes_owned_cleanup_and_preserves_shared_config() -> None:
    result = _run_uninstall(
        r'''
tmp=$(mktemp -d)
CADDY_CONFIG_PATH="$tmp/Caddyfile"
CADDY_APP_CONFIG="$tmp/simple-network-dashboard.caddy"
CADDY_IMPORT_LINE="import $CADDY_APP_CONFIG"
printf ':80 { respond "shared" }\nimport /etc/caddy/other.caddy\n%s\n%s\n' "$CADDY_IMPORT_COMMENT" "$CADDY_IMPORT_LINE" > "$CADDY_CONFIG_PATH"
printf '%s\nlocalhost { respond "dashboard" }\n' "$CADDY_APP_MARKER" > "$CADDY_APP_CONFIG"
stat() { printf '0:0:644\n'; }
caddy() { echo validated; }
inspect_caddy_for_cleanup() { CADDY_BINARY=caddy; }
systemctl() { echo reloaded; }
remove_dashboard_caddy_integration
cat "$CADDY_CONFIG_PATH"
[ ! -e "$CADDY_APP_CONFIG" ]
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    assert ':80 { respond "shared" }' in result.stdout
    assert "import /etc/caddy/other.caddy" in result.stdout
    assert "managed Caddy import" not in result.stdout
    assert "validated" in result.stdout
    assert "reloaded" in result.stdout
    assert result.stdout.index("validated") < result.stdout.index("reloaded")


@pytest.mark.parametrize("failure", ["publish", "reload"])
def test_uninstall_failure_restores_owned_proxy(failure: str) -> None:
    install_override = (
        'install() { return 1; }'
        if failure == "publish"
        else 'systemctl() { reload_count=$((reload_count + 1)); [ "$reload_count" -gt 1 ]; }'
    )
    result = _run_uninstall(
        f'''
tmp=$(mktemp -d)
CADDY_CONFIG_PATH="$tmp/Caddyfile"
CADDY_APP_CONFIG="$tmp/simple-network-dashboard.caddy"
CADDY_IMPORT_LINE="import $CADDY_APP_CONFIG"
printf ':80 {{ respond "shared" }}\n%s\n%s\n' "$CADDY_IMPORT_COMMENT" "$CADDY_IMPORT_LINE" > "$CADDY_CONFIG_PATH"
printf '%s\nlocalhost {{ respond "dashboard" }}\n' "$CADDY_APP_MARKER" > "$CADDY_APP_CONFIG"
cp "$CADDY_CONFIG_PATH" "$tmp/original-caddy"
cp "$CADDY_APP_CONFIG" "$tmp/original-app"
stat() {{ printf '0:0:644\n'; }}
caddy() {{ :; }}
inspect_caddy_for_cleanup() {{ CADDY_BINARY=caddy; }}
reload_count=0
{install_override}
if remove_dashboard_caddy_integration; then exit 10; fi
cmp "$CADDY_CONFIG_PATH" "$tmp/original-caddy"
cmp "$CADDY_APP_CONFIG" "$tmp/original-app"
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "restoring the dashboard proxy configuration" in result.stdout


def test_multiple_labelled_ufw_rules_delete_descending_and_failure_is_reported() -> None:
    result = _run_uninstall(
        r'''
ufw() {
    if [ "$1" = status ]; then
        printf '[ 4] 443/tcp ALLOW IN Anywhere (v6) # Simple Network Dashboard HTTPS\n'
        printf '[ 3] 22/tcp ALLOW IN Anywhere\n'
        printf '[ 2] 443/tcp ALLOW IN Anywhere # Simple Network Dashboard HTTPS\n'
    elif [ "$3" = 2 ]; then
        printf 'delete-failed-%s\n' "$3"
        return 1
    else
        printf 'deleted-%s\n' "$3"
    fi
}
if remove_dashboard_ufw_rules; then exit 10; fi
'''
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.index("deleted-4") < result.stdout.index("delete-failed-2")
    assert "deleted-3" not in result.stdout


def test_uninstall_refuses_bare_dashboard_import() -> None:
    result = _run_uninstall(
        r'''
tmp=$(mktemp -d)
CADDY_CONFIG_PATH="$tmp/Caddyfile"
CADDY_APP_CONFIG="$tmp/simple-network-dashboard.caddy"
CADDY_IMPORT_LINE="import $CADDY_APP_CONFIG"
printf '%s\n' "$CADDY_IMPORT_LINE" > "$CADDY_CONFIG_PATH"
printf '%s\n' "$CADDY_APP_MARKER" > "$CADDY_APP_CONFIG"
stat() { printf '0:0:644\n'; }
inspect_caddy_for_cleanup() { CADDY_BINARY=caddy; }
if remove_dashboard_caddy_integration; then exit 10; fi
grep -Fxq "$CADDY_IMPORT_LINE" "$CADDY_CONFIG_PATH"
[ -f "$CADDY_APP_CONFIG" ]
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "unowned dashboard import" in result.stdout


def test_uninstall_backup_copy_failure_stops_before_mutation() -> None:
    result = _run_uninstall(
        r'''
tmp=$(mktemp -d)
CADDY_CONFIG_PATH="$tmp/Caddyfile"
CADDY_APP_CONFIG="$tmp/simple-network-dashboard.caddy"
CADDY_IMPORT_LINE="import $CADDY_APP_CONFIG"
printf '%s\n%s\n' "$CADDY_IMPORT_COMMENT" "$CADDY_IMPORT_LINE" > "$CADDY_CONFIG_PATH"
printf '%s\n' "$CADDY_APP_MARKER" > "$CADDY_APP_CONFIG"
stat() { printf '0:0:644\n'; }
caddy() { :; }
inspect_caddy_for_cleanup() { CADDY_BINARY=caddy; }
cp() { return 1; }
mv() { echo unexpected-move; return 0; }
install() { echo unexpected-publication; return 0; }
if remove_dashboard_caddy_integration; then exit 10; fi
[ -f "$CADDY_APP_CONFIG" ]
rm -rf "$tmp"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "unexpected-move" not in result.stdout
    assert "unexpected-publication" not in result.stdout
