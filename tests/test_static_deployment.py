"""Executable deployment contracts for the Linux installer."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

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
def test_failed_package_swap_preserves_flat_layout_and_restores_service(failure: str) -> None:
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
SERVICE_NAME=simple-network-dashboard
DASHBOARD_WAS_ACTIVE=true
chown() {{ :; }}
chmod() {{ :; }}
systemctl() {{ printf 'systemctl %s\n' "$*" >> "$tmp/systemctl"; return 0; }}
{_flat_layout_setup()}
{copy_override}
{move_override}
if ! copy_application_files; then
  echo "Error: application files could not be copied."
  restore_previously_active_service
else
  exit 10
fi
for filename in {' '.join(LEGACY_MODULES)}; do test -f "$APP_DIR/$filename"; done
test -d "$APP_DIR/static"
test -d "$APP_DIR/{PACKAGE}"
test -f "$APP_DIR/{PACKAGE}/removed_module.py"
test ! -e "$APP_DIR/.simple_network_dashboard.new"
cat "$tmp/systemctl"
'''
    )

    assert result.returncode == 0, result.stderr
    assert "Error: application files could not be copied." in result.stdout
    assert "systemctl start simple-network-dashboard" in result.stdout
    assert "systemctl is-active --quiet simple-network-dashboard" in result.stdout


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


def test_copy_replaces_leftover_previous_package_directory() -> None:
    result = _run_install(
        f'''\
tmp=$(mktemp -d) || exit 78
trap 'rm -rf -- "$tmp"' EXIT
APP_DIR="$tmp/app"
chown() {{ :; }}
chmod() {{ :; }}
mkdir -p "$APP_DIR/{PACKAGE}" "$APP_DIR/.simple_network_dashboard.previous"
printf stale > "$APP_DIR/.simple_network_dashboard.previous/stale"
copy_application_files
test ! -e "$APP_DIR/.simple_network_dashboard.previous"
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


def test_main_flow_cleans_legacy_files_after_unit_reload_before_starting_backend() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")

    unit_write = installer.index("# Create systemd service file")
    reload = installer.index("systemctl daemon-reload", unit_write)
    cleanup = installer.index("if ! remove_legacy_application_files; then", reload)
    start = installer.index("if ! start_dashboard_backend; then", cleanup)
    assert unit_write < reload < cleanup < start
