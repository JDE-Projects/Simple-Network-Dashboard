"""Installer contract: install.sh refuses a python3 older than the supported minimum."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _find_bash() -> str | None:
    if os.name == "nt":
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        if git_bash.exists():
            return str(git_bash)
    return shutil.which("bash")


BASH = _find_bash()


def _check_with(python3_body: str) -> subprocess.CompletedProcess[str]:
    """Source install.sh, stand in a python3 that runs `python3_body`, and run the check."""
    if BASH is None:
        pytest.skip("Bash is required to exercise the installer's Python version check")

    script = f"""
source ./install.sh
python3() {{ {python3_body}; }}
check_python_version
"""
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("version", ["3.11", "3.12", "3.13", "4.0"])
def test_supported_python_passes(version: str) -> None:
    result = _check_with(f"echo {version}")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("version", ["3.10", "3.9", "2.7"])
def test_older_python_is_refused_with_its_version(version: str) -> None:
    result = _check_with(f"echo {version}")

    assert result.returncode == 1
    assert "Python 3.11 or newer is required" in result.stdout
    assert f"this system has Python {version}." in result.stdout


@pytest.mark.parametrize("python3_body", ["return 1", "echo abc", "echo 3", "echo 4", "echo 3.x", "echo ''"])
def test_unreadable_version_is_refused(python3_body: str) -> None:
    result = _check_with(python3_body)

    assert result.returncode == 1
    assert "could not read the version of python3" in result.stdout


def test_installer_checks_the_version_before_creating_anything() -> None:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    main_body = text[text.index("\nmain() {"):]

    check = main_body.index("if ! check_python_version; then")
    assert check < main_body.index('python3 -c "import venv"')
    assert check < main_body.index("classify_account_state")
    assert check < main_body.index('python3 -m venv "$APP_DIR/venv"')
