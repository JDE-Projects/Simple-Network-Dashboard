"""Executable static-asset deployment contracts for the Linux installer."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _find_bash() -> str | None:
    if os.name == "nt":
        git_bash = Path(r"C:\\Program Files\\Git\\bin\\bash.exe")
        if git_bash.exists():
            return str(git_bash)
    return shutil.which("bash")


BASH = _find_bash()


def _run_copy_application_files(existing_install: bool) -> subprocess.CompletedProcess[str]:
    if BASH is None:
        pytest.skip("Bash is required to exercise the installer copy contract")

    existing_files = ""
    if existing_install:
        existing_files = """
mkdir -p "$APP_DIR/static"
printf outdated > "$APP_DIR/static/index.html"
printf outdated > "$APP_DIR/main.py"
"""

    script = """
source ./install.sh
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT
APP_DIR="$TEST_ROOT/app"
{existing_files}
mkdir -p "$APP_DIR/static"
copy_application_files
diff -qr static "$APP_DIR/static"
for filename in main.py config.py metrics_poller.py ssh_manager.py auth.py session_manager.py requirements.txt uninstall.sh; do
  cmp -- "$filename" "$APP_DIR/$filename"
done
"""
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", script.format(existing_files=existing_files)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("existing_install", [False, True])
def test_installer_copies_complete_static_tree(existing_install: bool) -> None:
    result = _run_copy_application_files(existing_install)

    assert result.returncode == 0, result.stderr
