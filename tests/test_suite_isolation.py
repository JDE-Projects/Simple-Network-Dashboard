"""The test suite stays independent of host data and of test order."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from simple_network_dashboard import config, runtime_state


ROOT = Path(__file__).resolve().parents[1]
HEADER_TEST = "tests/test_http_protection.py::test_unhandled_error_response_keeps_security_headers"
SAVE_TEST = "tests/test_private_storage.py::test_devices_save_uses_private_temp_file_and_atomic_replace"
STORAGE_TEST = "tests/test_suite_isolation.py::test_storage_is_a_throwaway_folder"


def test_storage_is_a_throwaway_folder() -> None:
    for folder in (config.DATA_DIR, config.LOG_DIR):
        assert Path(folder).parent.name.startswith("snd-tests-")


def test_shared_state_change_made_by_one_test() -> None:
    runtime_state._recovery_mode = True
    runtime_state._devices_cache.append("left behind")


def test_shared_state_change_does_not_reach_the_next_test() -> None:
    assert runtime_state._recovery_mode is False
    assert runtime_state._devices_cache == []


def _make_malformed(devices_file: Path) -> None:
    devices_file.write_text("{not json", encoding="utf-8")


def _make_unreadable(devices_file: Path) -> None:
    devices_file.mkdir()


@pytest.mark.parametrize("damage", [_make_malformed, _make_unreadable])
@pytest.mark.parametrize("order", [(HEADER_TEST, SAVE_TEST), (SAVE_TEST, HEADER_TEST)])
def test_damaged_host_storage_does_not_affect_a_run(
    tmp_path: Path, damage, order: tuple[str, str]
) -> None:
    damage(tmp_path / "devices.json")
    env = dict(os.environ, SND_DATA_DIR=str(tmp_path), SND_LOG_DIR=str(tmp_path))

    result = subprocess.run(
        [sys.executable, "-m", "pytest", *order, STORAGE_TEST, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout
