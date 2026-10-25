"""Shared checks for the application's installed file layout."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import shlex

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "simple_network_dashboard"
PACKAGE_DIR = ROOT / PACKAGE
ENTRY_POINTS = ("__main__.py", "auth.py")
LOOSE_RUNTIME_FILES = ("snd-reset-password", "requirements.txt", "uninstall.sh")

_FUNCTION = re.compile(r"^copy_application_files\(\) \{\n(.*?)^\}", re.MULTILINE | re.DOTALL)
_DESTINATION = "$APP_DIR/"


def parse_copy_lines(installer_text: str) -> tuple[list[str], list[str]]:
    """Return the loose files and package folder copied by the installer.

    The parser accepts only plain ``cp`` commands with exact source names and
    destinations. Unsupported shell forms fail rather than being guessed.
    """
    installer_text = installer_text.replace("\r\n", "\n")
    match = _FUNCTION.search(installer_text)
    assert match, "install.sh has no copy_application_files function"
    body = match.group(1)
    assert "\\\n" not in body, "copy_application_files uses a line continuation, which this test does not parse"

    copy_lines = []
    for line in body.splitlines():
        tokens = shlex.split(line, comments=True)
        if tokens and tokens[0] == "cp" and tokens[-1] == _DESTINATION:
            copy_lines.append(tokens)
    assert len(copy_lines) == 2, f'expected two `cp ... "{_DESTINATION}"` lines in copy_application_files, found {len(copy_lines)}'

    loose_line = next((line for line in copy_lines if PACKAGE not in line), None)
    package_line = next((line for line in copy_lines if PACKAGE in line), None)
    assert loose_line and package_line, "copy_application_files must copy loose runtime files and the package folder"
    assert loose_line[1:-1] == list(LOOSE_RUNTIME_FILES), "loose runtime copy line does not match the installed runtime files"
    assert package_line[1:-1] == ["-R", "--no-preserve=ownership", PACKAGE], "package copy line must use cp -R --no-preserve=ownership"
    return loose_line[1:-1], package_line[1:-1]


def installer_copy_list() -> list[str]:
    """The loose runtime files install.sh copies into the install folder."""
    loose_files, _ = parse_copy_lines((ROOT / "install.sh").read_text(encoding="utf-8"))
    return loose_files


def app_modules() -> set[str]:
    """Return package modules reachable from the installed entry points."""
    package_modules = {path.stem for path in PACKAGE_DIR.glob("*.py")}
    found: set[str] = set()
    pending = list(ENTRY_POINTS)
    while pending:
        filename = pending.pop()
        if filename in found:
            continue
        found.add(filename)
        tree = ast.parse((PACKAGE_DIR / filename).read_text(encoding="utf-8"), filename=filename)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{filename} uses a relative import, which this test does not follow"
                names = [node.module]
            else:
                continue
            for name in names:
                assert name is not None, f"{filename} has an import without a module name"
                parts = name.split(".")
                if parts[0] == PACKAGE and len(parts) > 1:
                    module = parts[1]
                    assert module in package_modules, f"{filename} imports missing package module {name}"
                    pending.append(f"{module}.py")
    return found
