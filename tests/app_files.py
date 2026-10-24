"""Shared view of the files the installed app needs.

The installer's copy line in `copy_application_files` is the one list of
application files. Tests read it from install.sh instead of keeping their own
copies; test_installer_file_list.py checks that list against the modules the
app actually imports.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import shlex

ROOT = Path(__file__).resolve().parents[1]

# Files the installed system runs directly: systemd runs `python main.py`, and
# the installer and snd-reset-password run `auth.py`. A new directly run
# script must be added here.
ENTRY_POINTS = ("main.py", "auth.py")

_FUNCTION = re.compile(r"^copy_application_files\(\) \{\n(.*?)^\}", re.MULTILINE | re.DOTALL)
_DESTINATION = "$APP_DIR/"


def parse_copy_line(installer_text: str) -> list[str]:
    """Return the file names on the installer's application copy line.

    Only the plain form `cp <file> ... "$APP_DIR/"` is accepted. Anything
    this parser could misread (line continuations, options, variables, or
    wildcards among the files) fails loudly instead of being guessed at.
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
    assert len(copy_lines) == 1, (
        f'expected one `cp ... "{_DESTINATION}"` line in copy_application_files, found {len(copy_lines)}')

    files = copy_lines[0][1:-1]
    unsupported = [name for name in files if name.startswith("-") or any(c in name for c in "$*?[")]
    assert not unsupported, f"copy line has options, variables, or wildcards this test does not parse: {unsupported}"
    return files


def installer_copy_list() -> list[str]:
    """The application files install.sh copies into the install folder."""
    return parse_copy_line((ROOT / "install.sh").read_text(encoding="utf-8"))


def app_modules() -> set[str]:
    """Repo-root modules reachable by import from the entry points.

    Follows plain `import x` and `from x import y` statements anywhere in a
    file, including inside functions. Imports made at run time from a string
    (importlib, __import__) are not followed.
    """
    root_modules = {path.stem for path in ROOT.glob("*.py")}
    found: set[str] = set()
    pending = list(ENTRY_POINTS)
    while pending:
        filename = pending.pop()
        if filename in found:
            continue
        found.add(filename)
        tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"), filename=filename)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{filename} uses a relative import, which this test does not follow"
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in root_modules:
                    pending.append(f"{top}.py")
    return found
