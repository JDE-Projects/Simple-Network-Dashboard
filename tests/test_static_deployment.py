"""Executable deployment contracts for the Linux installer."""

from __future__ import annotations

import http.server
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

from app_files import PACKAGE, installer_copy_list


ROOT = Path(__file__).resolve().parents[1]
LEGACY_MODULES = (
    "auth.py", "config.py", "debug_log.py", "delivery_queue.py", "host_keys.py",
    "main.py", "metrics_poller.py", "persistence.py", "runtime_state.py",
    "session_manager.py", "ssh_manager.py", "ssh_session.py", "terminal_control.py",
    "terminal_history.py", "terminal_run.py", "update_check.py", "ws_manager.py",
)


def _find_bash() -> str | None:
    if os.name == "nt":
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        if git_bash.exists():
            return str(git_bash)
    return shutil.which("bash")


BASH = _find_bash()


def _run_install(body: str) -> subprocess.CompletedProcess[str]:
    if BASH is None:
        pytest.skip("Bash is required to exercise the installer copy contract")
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", f"source ./install.sh\n{body}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _flat_layout_setup() -> str:
    legacy_files = " ".join(LEGACY_MODULES)
    return f"""
mkdir -p "$APP_DIR/static" "$APP_DIR/__pycache__" "$APP_DIR/{PACKAGE}"
for filename in {legacy_files}; do
  printf outdated > "$APP_DIR/$filename"
done
printf outdated > "$APP_DIR/static/index.html"
printf stale > "$APP_DIR/{PACKAGE}/removed_module.py"
"""


def test_copy_keeps_legacy_layout_until_cleanup() -> None:
    result = _run_install(
        f'''\
tmp=$(mktemp -d) || exit 78
trap 'rm -rf -- "$tmp"' EXIT
APP_DIR="$tmp/app"
chown() {{ :; }}
chmod() {{ :; }}
{_flat_layout_setup()}
mkdir -p "$APP_DIR/venv"
printf keep > "$APP_DIR/unrelated-file"
copy_application_files
diff -qr {PACKAGE}/static "$APP_DIR/{PACKAGE}/static"
for filename in {' '.join(installer_copy_list())}; do
  cmp -- "$filename" "$APP_DIR/$filename"
done
for filename in {' '.join(LEGACY_MODULES)}; do test -f "$APP_DIR/$filename"; done
test -d "$APP_DIR/static"
test -d "$APP_DIR/__pycache__"
test -d "$APP_DIR/venv"
test -f "$APP_DIR/unrelated-file"
remove_legacy_application_files
for filename in {' '.join(LEGACY_MODULES)}; do test ! -e "$APP_DIR/$filename"; done
test ! -e "$APP_DIR/static"
test ! -e "$APP_DIR/__pycache__"
test -d "$APP_DIR/{PACKAGE}"
test -f "$APP_DIR/unrelated-file"
'''
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("failure", ["copy", "move"])
def test_failed_package_swap_preserves_flat_layout_and_leaves_no_staging(failure: str) -> None:
    copy_override = (
        '''
cp() {
  if [ "$1" = "-R" ]; then return 1; fi
  command cp "$@"
}
'''
        if failure == "copy"
        else ""
    )
    move_override = (
        '''
mv() {
  if [ "${@: -2:1}" = "$APP_DIR/.simple_network_dashboard.new" ]; then return 1; fi
  command mv "$@"
}
'''
        if failure == "move"
        else ""
    )
    result = _run_install(
        f'''\
tmp=$(mktemp -d) || exit 78
trap 'rm -rf -- "$tmp"' EXIT
APP_DIR="$tmp/app"
chown() {{ :; }}
chmod() {{ :; }}
{_flat_layout_setup()}
{copy_override}
{move_override}
if copy_application_files; then
  exit 10
fi
for filename in {' '.join(LEGACY_MODULES)}; do test -f "$APP_DIR/$filename"; done
test -d "$APP_DIR/static"
test ! -e "$APP_DIR/.simple_network_dashboard.new"
'''
    )

    assert result.returncode == 0, result.stderr


def test_copy_replaces_leftover_staging_directory() -> None:
    result = _run_install(
        f'''\
tmp=$(mktemp -d) || exit 78
trap 'rm -rf -- "$tmp"' EXIT
APP_DIR="$tmp/app"
chown() {{ :; }}
chmod() {{ :; }}
mkdir -p "$APP_DIR/.simple_network_dashboard.new"
printf stale > "$APP_DIR/.simple_network_dashboard.new/stale"
copy_application_files
test ! -e "$APP_DIR/.simple_network_dashboard.new"
test -f "$APP_DIR/{PACKAGE}/__main__.py"
test ! -e "$APP_DIR/{PACKAGE}/stale"
'''
    )

    assert result.returncode == 0, result.stderr


def test_copy_replaces_package_without_creating_or_touching_a_previous_copy() -> None:
    result = _run_install(
        f'''\
tmp=$(mktemp -d) || exit 78
trap 'rm -rf -- "$tmp"' EXIT
APP_DIR="$tmp/app"
chown() {{ :; }}
chmod() {{ :; }}
mkdir -p "$APP_DIR/{PACKAGE}" "$APP_DIR/.simple_network_dashboard.previous"
printf stale > "$APP_DIR/{PACKAGE}/removed_module.py"
printf keep > "$APP_DIR/.simple_network_dashboard.previous/keep"
copy_application_files
test "$(cat "$APP_DIR/.simple_network_dashboard.previous/keep")" = keep
test ! -e "$APP_DIR/{PACKAGE}/removed_module.py"
test -f "$APP_DIR/{PACKAGE}/__main__.py"
'''
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("legacy_kind", ["directory", "symlink"])
def test_legacy_cleanup_refuses_unsafe_legacy_module_path(legacy_kind: str) -> None:
    setup = (
        'mkdir -p "$APP_DIR/main.py"'
        if legacy_kind == "directory"
        else 'ln -s target "$APP_DIR/main.py" || exit 77'
    )
    result = _run_install(
        f'''\
tmp=$(mktemp -d) || exit 78
trap 'rm -rf -- "$tmp"' EXIT
APP_DIR="$tmp/app"
mkdir -p "$APP_DIR"
{setup}
if remove_legacy_application_files; then exit 10; fi
'''
    )

    if result.returncode == 77:
        pytest.skip("The current Windows test account cannot create symlinks")
    assert result.returncode == 0, result.stderr
    assert "Error: old application module" in result.stdout


# ---------------------------------------------------------------------------
# Update rollback and readiness
# ---------------------------------------------------------------------------

_UPDATE_HARNESS = r'''
tmp=$(mktemp -d) || exit 78
trap 'rm -rf -- "$tmp"' EXIT
APP_DIR="$tmp/app"
UNIT_DEST="$tmp/unit/dashboard.service"
RESET_COMMAND="$tmp/sbin/snd-reset-password"
SERVICE_NAME=simple-network-dashboard
PORT=3001
PUBLIC_ORIGIN=https://dash.lan
DASHBOARD_WAS_ACTIVE=true
READY_TIMEOUT_SECONDS=1
READY_POLL_SECONDS=0
PIP_RC=0
AUTH_RC=0
READY_RESULTS="0"
ROLLBACK="$APP_DIR.rollback"
OLD_UNIT='ExecStart=/x/python -m simple_network_dashboard --port 3005'
mkdir -p "$APP_DIR/@PACKAGE@" "$APP_DIR/venv/bin" "$tmp/unit" "$tmp/sbin"
printf old-package > "$APP_DIR/@PACKAGE@/old.py"
printf old-venv > "$APP_DIR/venv/marker"
printf old-req > "$APP_DIR/requirements.txt"
printf old-reset > "$APP_DIR/snd-reset-password"
printf old-uninstall > "$APP_DIR/uninstall.sh"
printf old-reset > "$RESET_COMMAND"
printf '%s' "$OLD_UNIT" > "$UNIT_DEST"

chown() { :; }
chmod() { :; }
mkdir() {
  printf 'mkdir %s\n' "$*" >> "$tmp/mkdir.log"
  # The requested mode is only logged: a real 0700 folder is unreadable to
  # a non-owner Windows test account.
  if [ "$1" = -m ]; then shift 2; fi
  command mkdir "$@"
}
systemctl() { printf 'systemctl %s\n' "$*" >> "$tmp/systemctl.log"; return 0; }
sudo() {
  shift 2
  case "$1" in
    */venv/bin/pip) echo pip >> "$tmp/pip.log"; return "$PIP_RC" ;;
    *) "$@" ;;
  esac
}
python3() { command mkdir -p "$3/bin" && printf new-venv > "$3/marker"; }
initialize_authentication() { return "$AUTH_RC"; }
install_reset_command() { cp "$APP_DIR/snd-reset-password" "$RESET_COMMAND"; }
wait_for_dashboard_ready() {
  printf 'ready %s\n' "$1" >> "$tmp/ready.log"
  local n results
  n=$(wc -l < "$tmp/ready.log")
  results=($READY_RESULTS)
  return "${results[$((n - 1))]:-0}"
}

check() { "$@" || { echo "CHECK FAILED: $*" >&2; exit 90; }; }
check_not() { if "$@"; then echo "CHECK FAILED (unexpected success): $*" >&2; exit 91; fi; }
content_is() { [ "$(cat "$1")" = "$2" ]; }
run_update() {
  detect_existing_installation
  if update_application; then rc=0; else rc=$?; fi
}
check_old_install_back() {
  check content_is "$APP_DIR/@PACKAGE@/old.py" old-package
  check content_is "$APP_DIR/venv/marker" old-venv
  check test ! -e "$APP_DIR/@PACKAGE@/__main__.py"
  check content_is "$APP_DIR/requirements.txt" old-req
  check content_is "$APP_DIR/snd-reset-password" old-reset
  check content_is "$RESET_COMMAND" old-reset
  check content_is "$UNIT_DEST" "$OLD_UNIT"
}
'''.replace("@PACKAGE@", PACKAGE)


def _run_update(body: str) -> subprocess.CompletedProcess[str]:
    return _run_install(_UPDATE_HARNESS + body)


def test_update_rollback_point_holds_package_venv_and_small_files() -> None:
    result = _run_update(
        r'''
detect_existing_installation
check test "$UPDATE_ELIGIBLE" = true
create_rollback_point
check test -f "$ROLLBACK/complete"
check content_is "$ROLLBACK/@PACKAGE@/old.py" old-package
check content_is "$ROLLBACK/venv/marker" old-venv
check test ! -e "$APP_DIR/@PACKAGE@"
check test ! -e "$APP_DIR/venv"
check cmp -s -- "$ROLLBACK/files/requirements" "$APP_DIR/requirements.txt"
check cmp -s -- "$ROLLBACK/files/app-reset" "$APP_DIR/snd-reset-password"
check cmp -s -- "$ROLLBACK/files/uninstall" "$APP_DIR/uninstall.sh"
check cmp -s -- "$ROLLBACK/files/reset-command" "$RESET_COMMAND"
check cmp -s -- "$ROLLBACK/files/unit" "$UNIT_DEST"
check content_is "$ROLLBACK/port" 3005
check grep -qx 'unit present' "$ROLLBACK/manifest"
check grep -qx 'package present' "$ROLLBACK/manifest"
check grep -q -- "mkdir -m 0700 -- $ROLLBACK\$" "$tmp/mkdir.log"
'''.replace("@PACKAGE@", PACKAGE)
    )

    assert result.returncode == 0, result.stderr


def test_rollback_point_build_failure_puts_back_moved_items() -> None:
    result = _run_update(
        r'''
detect_existing_installation
mv() {
  if [ "${@: -2:1}" = "$APP_DIR/venv" ]; then return 1; fi
  command mv "$@"
}
if create_rollback_point; then exit 10; fi
unset -f mv
check content_is "$APP_DIR/@PACKAGE@/old.py" old-package
check content_is "$APP_DIR/venv/marker" old-venv
check test ! -e "$ROLLBACK"
'''.replace("@PACKAGE@", PACKAGE)
    )

    assert result.returncode == 0, result.stderr
    assert "could not move the Python environment" in result.stdout


def test_pip_failure_restores_previous_package_and_venv() -> None:
    result = _run_update(
        r'''
PIP_RC=1
READY_RESULTS="0 0"
run_update
check test "$rc" -ne 0
check_old_install_back
check content_is "$APP_DIR/uninstall.sh" old-uninstall
check test ! -e "$ROLLBACK"
exit "$rc"
'''
    )

    assert result.returncode == 1, result.stderr
    assert "the dashboard's Python packages could not be installed" in result.stdout
    assert "Previous version restored and responding." in result.stdout


def test_auth_validation_failure_restores_previous_install() -> None:
    result = _run_update(
        r'''
AUTH_RC=1
run_update
check test "$rc" -ne 0
check_old_install_back
check test ! -e "$ROLLBACK"
exit "$rc"
'''
    )

    assert result.returncode == 1, result.stderr
    assert "Previous version restored and responding." in result.stdout


def test_start_failure_restores_previous_install() -> None:
    result = _run_update(
        r'''
READY_RESULTS="1 0"
run_update
check test "$rc" -ne 0
check_old_install_back
check test ! -e "$ROLLBACK"
exit "$rc"
'''
    )

    assert result.returncode == 1, result.stderr
    assert "did not become ready" in result.stdout
    assert "Previous version restored and responding." in result.stdout


def test_failed_restore_keeps_rollback_folder_and_reports_it() -> None:
    result = _run_update(
        r'''
PIP_RC=1
READY_RESULTS="1"
run_update
check test "$rc" -ne 0
check test -f "$ROLLBACK/complete"
check_old_install_back
echo "ROLLBACK_PATH=$ROLLBACK"
exit "$rc"
'''
    )

    assert result.returncode == 1, result.stderr
    rollback = next(
        line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("ROLLBACK_PATH=")
    )
    assert f"The saved copy was kept at: {rollback}" in result.stdout
    assert "restored and responding" not in result.stdout


def test_restore_recreates_original_absence_of_small_file() -> None:
    result = _run_update(
        r'''
rm -f -- "$APP_DIR/uninstall.sh"
PIP_RC=1
run_update
check test "$rc" -ne 0
check test ! -e "$APP_DIR/uninstall.sh"
check test ! -e "$ROLLBACK"
exit "$rc"
'''
    )

    assert result.returncode == 1, result.stderr
    assert "Previous version restored and responding." in result.stdout


def test_rollback_manifest_records_absent_small_file() -> None:
    result = _run_update(
        r'''
rm -f -- "$UNIT_DEST"
detect_existing_installation
create_rollback_point
check grep -qx 'unit absent' "$ROLLBACK/manifest"
check test ! -e "$ROLLBACK/files/unit"
check content_is "$ROLLBACK/port" ""
'''
    )

    assert result.returncode == 0, result.stderr


def test_restore_restores_both_reset_command_copies() -> None:
    result = _run_update(
        r'''
printf old-wrapper > "$APP_DIR/snd-reset-password"
printf old-wrapper > "$RESET_COMMAND"
AUTH_RC=1
run_update
check test "$rc" -ne 0
check content_is "$APP_DIR/snd-reset-password" old-wrapper
check content_is "$RESET_COMMAND" old-wrapper
check cmp -s -- "$APP_DIR/snd-reset-password" "$RESET_COMMAND"
'''
    )

    assert result.returncode == 0, result.stderr


def test_restore_probes_previous_port() -> None:
    result = _run_update(
        r'''
PORT=3999
READY_RESULTS="1 0"
run_update
check test "$rc" -ne 0
check test "$(cat "$tmp/ready.log")" = "ready 3999
ready 3005"
'''
    )

    assert result.returncode == 0, result.stderr


def test_restore_resets_failed_state_before_start() -> None:
    result = _run_update(
        r'''
PIP_RC=1
run_update
check test "$rc" -ne 0
check test "$(cat "$tmp/systemctl.log")" = "systemctl stop simple-network-dashboard
systemctl daemon-reload
systemctl reset-failed simple-network-dashboard
systemctl start simple-network-dashboard"
'''
    )

    assert result.returncode == 0, result.stderr


def test_restore_leaves_service_stopped_when_it_was_not_active() -> None:
    result = _run_update(
        r'''
DASHBOARD_WAS_ACTIVE=false
PIP_RC=1
run_update
check test "$rc" -ne 0
check_old_install_back
check test ! -e "$ROLLBACK"
check test ! -e "$tmp/ready.log"
check_not grep -q 'systemctl start' "$tmp/systemctl.log"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "left stopped, as it was before the update" in result.stdout


def test_legacy_flat_layout_update_restores_without_a_package_folder() -> None:
    result = _run_update(
        r'''
rm -r -- "$APP_DIR/@PACKAGE@"
printf legacy > "$APP_DIR/main.py"
detect_existing_installation
check test "$UPDATE_ELIGIBLE" = true
check test "$PACKAGE_WAS_INSTALLED" = false
PIP_RC=1
run_update
check test "$rc" -ne 0
check test ! -e "$APP_DIR/@PACKAGE@"
check content_is "$APP_DIR/main.py" legacy
check content_is "$APP_DIR/venv/marker" old-venv
check test ! -e "$ROLLBACK"
'''.replace("@PACKAGE@", PACKAGE)
    )

    assert result.returncode == 0, result.stderr


def test_leftover_rollback_folder_refuses_before_stopping_service() -> None:
    result = _run_update(
        r'''
command mkdir -p "$ROLLBACK"
printf keep > "$ROLLBACK/saved"
if refuse_leftover_rollback_folder; then exit 10; fi
check content_is "$ROLLBACK/saved" keep
check test ! -e "$tmp/systemctl.log"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "a previous update did not finish" in result.stdout
    assert "saved" in result.stdout
    assert "sudo mv -T --" in result.stdout

    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    main_body = installer[installer.index("\nmain() {"):]
    leftover = main_body.index("if ! refuse_leftover_rollback_folder; then")
    assert leftover < main_body.index("if ! preflight_https; then")
    assert leftover < main_body.index('systemctl stop "$SERVICE_NAME"')


def test_low_disk_space_refuses_before_stopping_service() -> None:
    result = _run_update(
        r'''
du() { printf '50000\t%s\n' "$3"; }
df() { printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/x 9000000 1 %s 1%% /\n' "$FREE_KB"; }
FREE_KB=100000
if check_disk_space_for_update; then exit 10; fi
check test ! -e "$tmp/systemctl.log"
FREE_KB=5000000
check check_disk_space_for_update
'''
    )

    assert result.returncode == 0, result.stderr
    assert "not enough free disk space" in result.stdout

    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    main_body = installer[installer.index("\nmain() {"):]
    assert main_body.index("check_disk_space_for_update") < main_body.index('systemctl stop "$SERVICE_NAME"')


_READINESS_STUBS = r'''
tmp=$(mktemp -d) || exit 78
trap 'rm -rf -- "$tmp"' EXIT
SERVICE_NAME=simple-network-dashboard
READY_TIMEOUT_SECONDS=1
READY_POLL_SECONDS=0
probe_n=0
dashboard_probe() { local rc="${PROBE_RC[$probe_n]:-1}"; probe_n=$((probe_n + 1)); return "$rc"; }
systemctl() {
  case "$1" in
    show)
      echo x >> "$tmp/show.log"
      local n
      n=$(wc -l < "$tmp/show.log")
      if [ "$n" -ge "$RESTART_CHANGES_AT" ]; then echo 2; else echo 0; fi
      ;;
    is-active) return "${ACTIVE_RC:-0}" ;;
  esac
  return 0
}
RESTART_CHANGES_AT=99
'''


def test_readiness_accepts_a_steady_service() -> None:
    result = _run_install(
        _READINESS_STUBS
        + r'''
PROBE_RC=(0 0 0 0)
wait_for_dashboard_ready 3000
test "$probe_n" -eq 4
'''
    )

    assert result.returncode == 0, result.stderr


def test_readiness_rejects_restart_count_change() -> None:
    result = _run_install(
        _READINESS_STUBS
        + r'''
PROBE_RC=(0 0 0 0)
RESTART_CHANGES_AT=2
if wait_for_dashboard_ready 3000; then exit 10; fi
'''
    )

    assert result.returncode == 0, result.stderr
    assert "restarted while it was being checked" in result.stdout


def test_readiness_rejects_a_service_that_is_not_active_at_the_end() -> None:
    result = _run_install(
        _READINESS_STUBS
        + r'''
PROBE_RC=(0 0 0 0)
ACTIVE_RC=3
if wait_for_dashboard_ready 3000; then exit 10; fi
'''
    )

    assert result.returncode == 0, result.stderr
    assert "is not running" in result.stdout


def test_readiness_requires_consecutive_successes() -> None:
    result = _run_install(
        _READINESS_STUBS
        + r'''
READY_TIMEOUT_SECONDS=2
PROBE_RC=(0 0 1 0 0 0 0)
wait_for_dashboard_ready 3000
test "$probe_n" -eq 7
READY_TIMEOUT_SECONDS=1
probe_n=0
PROBE_RC=(0 0 0 1 0 0 0 1 0 0 0 1)
if wait_for_dashboard_ready 3000; then exit 10; fi
'''
    )

    assert result.returncode == 0, result.stderr
    assert "did not answer steadily" in result.stdout


def test_readiness_refuses_an_unknown_port() -> None:
    result = _run_install(
        _READINESS_STUBS
        + r'''
PROBE_RC=(0 0 0 0)
if wait_for_dashboard_ready ""; then exit 10; fi
test "$probe_n" -eq 0
'''
    )

    assert result.returncode == 0, result.stderr


def _serve(status: int, location: str | None = None) -> http.server.ThreadingHTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            self.send_response(status)
            if location is not None:
                self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize(
    ("status", "location", "expected"), [(200, None, 0), (302, "/elsewhere", 1), (500, None, 1)]
)
def test_readiness_probe_accepts_only_a_direct_200(
    tmp_path: Path, status: int, location: str | None, expected: int
) -> None:
    launcher = tmp_path / "app" / "venv" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    server = _serve(status, location)
    try:
        port = server.server_address[1]
        result = _run_install(
            f'APP_DIR="{tmp_path / "app"}"\n'
            "READY_REQUEST_TIMEOUT_SECONDS=2\n"
            f"if dashboard_probe {port}; then echo accepted; else echo rejected; fi\n"
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("accepted" if expected == 0 else "rejected")


def test_fresh_install_failure_keeps_report_and_exit() -> None:
    result = _run_update(
        r'''
rm -r -- "$APP_DIR" "$UNIT_DEST"
command mkdir -p "$APP_DIR"
DASHBOARD_WAS_ACTIVE=false
PIP_RC=1
run_update
check test "$rc" -ne 0
check test "$UPDATE_ELIGIBLE" = false
check test ! -e "$ROLLBACK"
check_not grep -q rollback "$tmp/mkdir.log"
check test ! -e "$tmp/systemctl.log"
exit "$rc"
'''
    )

    assert result.returncode == 1, result.stderr
    assert "the dashboard's Python packages could not be installed" in result.stdout
    assert "Restoring" not in result.stdout


def test_success_deletes_rollback_point_and_keeps_legacy_files_until_readiness_passed() -> None:
    result = _run_update(
        r'''
printf legacy > "$APP_DIR/main.py"
READY_RESULTS="0"
run_update
check test "$rc" -eq 0
check test ! -e "$ROLLBACK"
check content_is "$APP_DIR/venv/marker" new-venv
check test -f "$APP_DIR/@PACKAGE@/__main__.py"
check grep -q -- "--port 3001" "$UNIT_DEST"
check test "$(cat "$tmp/ready.log")" = "ready 3001"
check test -f "$APP_DIR/main.py"
'''.replace("@PACKAGE@", PACKAGE)
    )

    assert result.returncode == 0, result.stderr


def test_main_flow_cleans_legacy_files_after_readiness_and_rollback_deletion() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8").replace("\r\n", "\n")

    apply_new_version = installer[installer.index("apply_new_version() {"):installer.index("update_application() {")]
    unit_write = apply_new_version.index("if ! write_service_unit; then")
    reload = apply_new_version.index("systemctl daemon-reload", unit_write)
    enable = apply_new_version.index('systemctl enable "$SERVICE_NAME"', reload)
    start = apply_new_version.index("if ! start_dashboard_backend; then", enable)
    assert unit_write < reload < enable < start

    update_application = installer[installer.index("update_application() {"):installer.index("\nmain() {")]
    assert update_application.index("apply_new_version") < update_application.index("if ! discard_rollback_point; then")

    main_body = installer[installer.index("\nmain() {"):]
    update = main_body.index("if ! update_application; then")
    cleanup = main_body.index("if ! remove_legacy_application_files; then")
    proxy = main_body.index("if ! write_caddy_proxy_config; then")
    assert update < cleanup < proxy


def test_main_flow_restores_previous_version_when_a_step_fails() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8").replace("\r\n", "\n")
    apply_new_version = installer[installer.index("apply_new_version() {"):installer.index("update_application() {")]

    for command, error in (
        ("copy_application_files", "Error: application files could not be copied."),
        ('sudo -u snd python3 -m venv "$APP_DIR/venv"', "Error: the Python virtual environment could not be created."),
        ('sudo -u snd "$APP_DIR/venv/bin/pip" install', "Error: the dashboard's Python packages could not be installed."),
        ("initialize_authentication", None),
        ("install_reset_command", "Error: dashboard password-reset command installation failed."),
        ("write_service_unit", "Error: the systemd service file could not be written."),
        ("systemctl daemon-reload", "Error: systemd could not reload the service definition."),
        ('systemctl enable "$SERVICE_NAME"', "Error: the dashboard service could not be enabled."),
        ("start_dashboard_backend", None),
    ):
        step = apply_new_version.index(f"if ! {command}")
        block = apply_new_version[step:apply_new_version.index("\n    fi\n", step)]
        assert "return 1" in block, command
        if error is not None:
            assert error in block, command

    update_application = installer[installer.index("update_application() {"):installer.index("\nmain() {")]
    failure = update_application.index("if ! apply_new_version; then")
    assert "restore_after_failed_update" in update_application[failure:update_application.index("return 1", failure)]


def test_uninstaller_removes_leftover_rollback_folder() -> None:
    if BASH is None:
        pytest.skip("Bash is required to exercise the uninstaller")
    result = subprocess.run(
        [
            BASH, "--noprofile", "--norc", "-c",
            r'''
source ./uninstall.sh
tmp=$(mktemp -d) || exit 78
trap 'rm -rf -- "$tmp"' EXIT
APP_DIR="$tmp/app"
mkdir -p "$APP_DIR" "$APP_DIR.rollback/venv"
printf saved > "$APP_DIR.rollback/venv/marker"
rollback_folder_is_safe_to_remove
remove_rollback_folder
test ! -e "$APP_DIR.rollback"
test -d "$APP_DIR"
remove_rollback_folder
''',
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Removed the leftover update rollback folder" in result.stdout


def test_uninstaller_refuses_a_rollback_path_that_is_not_a_folder() -> None:
    if BASH is None:
        pytest.skip("Bash is required to exercise the uninstaller")
    result = subprocess.run(
        [
            BASH, "--noprofile", "--norc", "-c",
            r'''
source ./uninstall.sh
tmp=$(mktemp -d) || exit 78
trap 'rm -rf -- "$tmp"' EXIT
APP_DIR="$tmp/app"
mkdir -p "$APP_DIR"
printf notadir > "$APP_DIR.rollback"
if rollback_folder_is_safe_to_remove; then exit 10; fi
test -f "$APP_DIR.rollback"
''',
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
