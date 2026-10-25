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
        git_bash = Path(r"C:\\Program Files\\Git\\bin\\bash.exe")
        if git_bash.exists():
            return str(git_bash)
    return shutil.which("bash")


BASH = _find_bash()


def _run_copy_application_files(tmp_path: Path, existing_install: bool) -> subprocess.CompletedProcess[str]:
    if BASH is None:
        pytest.skip("Bash is required to exercise the installer copy contract")

    existing_files = ""
    if existing_install:
        legacy_files = " ".join(LEGACY_MODULES)
        existing_files = f"""
mkdir -p "$APP_DIR/static" "$APP_DIR/__pycache__" "$APP_DIR/{PACKAGE}"
for filename in {legacy_files}; do
  printf outdated > "$APP_DIR/$filename"
done
printf outdated > "$APP_DIR/static/index.html"
printf stale > "$APP_DIR/{PACKAGE}/removed_module.py"
"""

    script = f"""
source ./install.sh
chown() {{ :; }}
chmod() {{ :; }}
APP_DIR="$TEST_ROOT/app"
{existing_files}
mkdir -p "$APP_DIR/venv"
printf keep > "$APP_DIR/snd-reset-password"
printf keep > "$APP_DIR/requirements.txt"
printf keep > "$APP_DIR/uninstall.sh"
printf keep > "$APP_DIR/unrelated-file"
copy_application_files
diff -qr {PACKAGE}/static "$APP_DIR/{PACKAGE}/static"
for filename in {' '.join(installer_copy_list())}; do
  cmp -- "$filename" "$APP_DIR/$filename"
done
test -d "$APP_DIR/venv"
test -f "$APP_DIR/snd-reset-password"
test -f "$APP_DIR/requirements.txt"
test -f "$APP_DIR/uninstall.sh"
test -f "$APP_DIR/unrelated-file"
"""
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", script],
        cwd=ROOT,
        env={**os.environ, "TEST_ROOT": tmp_path.as_posix()},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("existing_install", [False, True])
def test_installer_copies_complete_package_tree(tmp_path: Path, existing_install: bool) -> None:
    result = _run_copy_application_files(tmp_path, existing_install)

    assert result.returncode == 0, result.stderr


def test_installer_removes_only_old_application_layout(tmp_path: Path) -> None:
    result = _run_copy_application_files(tmp_path, existing_install=True)
    app_dir = tmp_path / "app"

    assert result.returncode == 0, result.stderr
    assert not (app_dir / "static").exists()
    assert not (app_dir / "__pycache__").exists()
    assert not (app_dir / PACKAGE / "removed_module.py").exists()
    assert all(not (app_dir / module).exists() for module in LEGACY_MODULES)
    assert (app_dir / "venv").is_dir()
    assert (app_dir / "snd-reset-password").is_file()
    assert (app_dir / "requirements.txt").is_file()
    assert (app_dir / "uninstall.sh").is_file()
    assert (app_dir / "unrelated-file").is_file()


@pytest.mark.parametrize("legacy_kind", ["directory", "symlink"])
def test_installer_refuses_unsafe_legacy_module_path(legacy_kind: str) -> None:
    if BASH is None:
        pytest.skip("Bash is required to exercise the installer copy contract")

    setup = (
        'mkdir -p "$APP_DIR/main.py"'
        if legacy_kind == "directory"
        else 'ln -s target "$APP_DIR/main.py" || exit 77'
    )
    script = f'''
source ./install.sh
chown() {{ :; }}
chmod() {{ :; }}
tmp=$(mktemp -d) || exit 78
trap 'rm -rf -- "$tmp"' EXIT
APP_DIR="$tmp/app"
mkdir -p "$APP_DIR"
{setup}
if copy_application_files; then exit 10; fi
'''
    result = subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode == 77:
        pytest.skip("The current Windows test account cannot create symlinks")
    assert result.returncode == 0, result.stderr
    assert "Error: old application module" in result.stdout
