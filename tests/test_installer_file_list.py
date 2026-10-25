"""The installer copies the package that contains every imported app module."""

from __future__ import annotations

import pytest

from app_files import LOOSE_RUNTIME_FILES, PACKAGE, PACKAGE_DIR, ROOT, app_modules, installer_copy_list, parse_copy_lines


def test_import_search_reaches_indirect_modules():
    modules = app_modules()
    assert {"__main__.py", "auth.py", "main.py", "ssh_manager.py", "ssh_session.py"} <= modules


def test_reachable_modules_live_in_installed_package():
    missing = sorted(module for module in app_modules() if not (PACKAGE_DIR / module).is_file())
    assert not missing, f"package is missing reachable app modules: {missing}"


def test_installer_copies_package_and_loose_runtime_files():
    loose_files, package_copy = parse_copy_lines((ROOT / "install.sh").read_text(encoding="utf-8"))
    assert loose_files == list(LOOSE_RUNTIME_FILES)
    assert package_copy[-1] == PACKAGE
    assert installer_copy_list() == list(LOOSE_RUNTIME_FILES)


def test_installer_copy_list_names_existing_files():
    absent = [name for name in installer_copy_list() if not (ROOT / name).is_file()]
    assert not absent, f"install.sh copies files that do not exist: {absent}"


def _installer(copy_body: str) -> str:
    return f'copy_application_files() {{\n{copy_body}\n}}\n'


@pytest.mark.parametrize("copy_body", [
    '    cp snd-reset-password \\\n+        requirements.txt "$APP_DIR/"\n    cp -R --no-preserve=ownership simple_network_dashboard "$APP_DIR/"',
    '    cp snd-reset-password $EXTRA "$APP_DIR/"\n    cp -R --no-preserve=ownership simple_network_dashboard "$APP_DIR/"',
    '    cp snd-reset-password requirements.txt "$APP_DIR/"\n    cp -R simple_network_dashboard "$APP_DIR/"',
    '    cp snd-reset-password requirements.txt "$APP_DIR/"\n    cp -R --no-preserve=ownership simple_network_dashboard "$OTHER/"',
    '    cp snd-reset-password requirements.txt "$APP_DIR/"\n    cp -R --no-preserve=ownership simple_network_dashboard "$staged_package"\n    cp unexpected "$APP_DIR/"',
])
def test_parser_refuses_forms_it_cannot_read(copy_body):
    with pytest.raises(AssertionError):
        parse_copy_lines(_installer(copy_body))
