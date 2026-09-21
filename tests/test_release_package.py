"""Guards the contents of the release tarball.

The release is packaged by the Build-Tools workflow as
`git archive --format=tar.gz --prefix=<name>/ HEAD`, so the archive contents
equal the git-tracked files at HEAD. This test builds the real archive with
`git archive` rather than trusting `git ls-files`, so it stays correct even if
.gitattributes export-ignore rules are added later.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = [
    "main.py",
    "config.py",
    "ws_manager.py",
    "update_check.py",
    "runtime_state.py",
    "debug_log.py",
    "metrics_poller.py",
    "ssh_manager.py",
    "auth.py",
    "session_manager.py",
    "snd-reset-password",
    "install.sh",
    "uninstall.sh",
    "requirements.txt",
    "requirements.in",
    "README.md",
    "LICENSE",
    "THIRD-PARTY-LICENSES.txt",
    "static/index.html",
    "static/favicon.svg",
]

FORBIDDEN_FILES = [
    "devices.json",
    "known_hosts",
    "CLAUDE.md",
    "BLUEPRINT.md",
    "READY.md",
    "NEEDS-FIXED.md",
    "ROADMAP.md",
]

FORBIDDEN_NAME_PATTERNS = [
    "Debug_Log_*.txt",
    "*_Console_*.txt",
]


def _archive_members() -> set[str]:
    with tempfile.TemporaryDirectory() as tmpdir:
        archive_path = Path(tmpdir) / "release.tar"
        subprocess.run(
            ["git", "archive", "--format=tar", "-o", str(archive_path), "HEAD"],
            cwd=ROOT,
            check=True,
        )
        with tarfile.open(archive_path, "r") as tar:
            names = tar.getnames()
    return {name.replace("\\", "/") for name in names}


MEMBERS = _archive_members()


def test_required_files_are_present():
    missing = [f for f in REQUIRED_FILES if f not in MEMBERS]
    assert not missing, f"Missing required files from release archive: {missing}"


def test_static_fonts_directory_present():
    font_members = [m for m in MEMBERS if m.startswith("static/fonts/") and not m.endswith("/")]
    assert font_members, "No files found under static/fonts/ in release archive"


def test_forbidden_files_are_absent():
    present = [f for f in FORBIDDEN_FILES if f in MEMBERS]
    assert not present, f"Private/ignored files leaked into release archive: {present}"


def test_no_debug_log_or_console_files():
    offenders = [
        m for m in MEMBERS
        if Path(m).match("Debug_Log_*.txt") or Path(m).match("*_Console_*.txt")
    ]
    assert not offenders, f"Debug or console log files leaked into release archive: {offenders}"


def test_no_bytecode_or_venv_artifacts():
    offenders = [
        m for m in MEMBERS
        if m.endswith(".pyc") or "__pycache__/" in m or ".venv/" in m
    ]
    assert not offenders, f"Bytecode or venv artifacts leaked into release archive: {offenders}"
