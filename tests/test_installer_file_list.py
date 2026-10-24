"""The installer copies every file the installed app needs.

A module left off the installer's copy line passes every other test and stops
the installed app from starting, so the line is checked against the modules
the app actually imports.
"""

from __future__ import annotations

import pytest

from app_files import ROOT, app_modules, installer_copy_list, parse_copy_line

# Runtime files that are not Python modules, so the import search cannot find them.
NON_MODULE_FILES = ("snd-reset-password", "requirements.txt", "uninstall.sh")


def test_import_search_reaches_indirect_modules():
    # ssh_session.py is reached only through main.py -> ssh_manager.py, so a
    # search that stopped following imports would miss it.
    modules = app_modules()
    assert {"main.py", "auth.py", "ssh_manager.py", "ssh_session.py"} <= modules


def test_installer_copies_every_app_module():
    missing = sorted(app_modules() - set(installer_copy_list()))
    assert not missing, f"install.sh copy_application_files does not copy: {missing}"


def test_installer_copies_non_module_runtime_files():
    missing = [name for name in NON_MODULE_FILES if name not in installer_copy_list()]
    assert not missing, f"install.sh copy_application_files does not copy: {missing}"


def test_installer_copy_list_names_existing_files():
    absent = [name for name in installer_copy_list() if not (ROOT / name).is_file()]
    assert not absent, f"install.sh copies files that do not exist: {absent}"


def _installer(copy_body: str) -> str:
    return f'copy_application_files() {{\n{copy_body}\n    cp -R static/. "$APP_DIR/static/"\n}}\n'


def test_parser_reads_the_plain_copy_line():
    assert parse_copy_line(_installer('    cp main.py auth.py "$APP_DIR/"')) == ["main.py", "auth.py"]


@pytest.mark.parametrize("copy_body", [
    '    cp main.py \\\n        auth.py "$APP_DIR/"',
    '    cp main.py $EXTRA "$APP_DIR/"',
    '    cp *.py "$APP_DIR/"',
    '    cp -p main.py "$APP_DIR/"',
    '    cp main.py "$APP_DIR/"\n    cp auth.py "$APP_DIR/"',
    '    install -m 0644 main.py "$APP_DIR/"',
])
def test_parser_refuses_forms_it_cannot_read(copy_body):
    with pytest.raises(AssertionError):
        parse_copy_line(_installer(copy_body))
