"""Offline contracts for the checked-in runtime dependency lock."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIREMENT_START = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^]]+\])?==")
SHA256_HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")


def _lock_entries() -> list[list[str]]:
    entries: list[list[str]] = []
    current: list[str] | None = None

    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        if not line[0].isspace():
            if current is not None:
                entries.append(current)
            current = [line]
        elif current is not None:
            current.append(line.strip())

    if current is not None:
        entries.append(current)
    return entries


def _direct_distribution_names() -> set[str]:
    names = set()
    for line in (ROOT / "requirements.in").read_text(encoding="utf-8").splitlines():
        match = REQUIREMENT_START.match(line)
        if match:
            names.add(match.group(1).lower())
    return names


def test_runtime_lock_pins_and_hashes_every_requirement() -> None:
    entries = _lock_entries()

    assert entries
    for entry in entries:
        assert REQUIREMENT_START.match(entry[0]), entry[0]
        assert any(SHA256_HASH.search(line) for line in entry), entry[0]


def test_runtime_lock_contains_all_direct_dependencies() -> None:
    locked_names = {
        REQUIREMENT_START.match(entry[0]).group(1).lower()
        for entry in _lock_entries()
        if REQUIREMENT_START.match(entry[0])
    }

    assert _direct_distribution_names() <= locked_names


def test_installer_requires_hashes_for_runtime_lock() -> None:
    install_script = (ROOT / "install.sh").read_text(encoding="utf-8")
    expected = (
        'sudo -u snd "$APP_DIR/venv/bin/pip" install --require-hashes '
        '-r "$APP_DIR/requirements.txt" --quiet'
    )

    assert expected in install_script
