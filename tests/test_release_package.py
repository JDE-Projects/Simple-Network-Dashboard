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

from app_files import installer_copy_list

ROOT = Path(__file__).resolve().parents[1]

# Everything the installer copies, plus the release files it does not.
REQUIRED_FILES = installer_copy_list() + [
    "install.sh",
    "requirements.in",
    "README.md",
    "LICENSE",
    "THIRD-PARTY-LICENSES.txt",
    "simple_network_dashboard/__init__.py",
    "simple_network_dashboard/__main__.py",
    "simple_network_dashboard/static/index.html",
    "simple_network_dashboard/static/favicon.svg",
    "simple_network_dashboard/static/fonts/OFL.txt",
    "simple_network_dashboard/static/vendor/xterm/xterm.js",
    "simple_network_dashboard/static/vendor/xterm/xterm.css",
    "simple_network_dashboard/static/vendor/xterm/addon-fit.js",
    "simple_network_dashboard/static/vendor/xterm/LICENSE-xterm.txt",
    "simple_network_dashboard/static/vendor/xterm/LICENSE-addon-fit.txt",
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
    font_members = [m for m in MEMBERS if m.startswith("simple_network_dashboard/static/fonts/") and not m.endswith("/")]
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
