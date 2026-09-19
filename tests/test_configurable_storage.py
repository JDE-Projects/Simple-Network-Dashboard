"""Executable contract for the configurable runtime storage roots.

DATA_DIR and LOG_DIR default to the installed Linux locations but can be pointed
elsewhere with the SND_DATA_DIR / SND_LOG_DIR environment variables, so the app
and its SSH host-key store can run off Linux for local development and tests.
Each case runs in a fresh subprocess so the module-level constants are evaluated
against that process's environment, not this test runner's.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]

_PROBE = (
    "import main, ssh_manager;"
    "print(main.DATA_DIR);"
    "print(main.LOG_DIR);"
    "print(ssh_manager.DATA_DIR)"
)


def _probe(env: dict[str, str]) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def test_env_override_redirects_all_storage_roots() -> None:
    env = dict(os.environ)
    env["SND_DATA_DIR"] = "/tmp/snd-test-data"
    env["SND_LOG_DIR"] = "/tmp/snd-test-log"
    main_data, main_log, ssh_data = _probe(env)
    assert main_data == "/tmp/snd-test-data"
    assert main_log == "/tmp/snd-test-log"
    # main.py and ssh_manager.py must resolve the same data root so known_hosts
    # stays with devices.json and the rest of the private data.
    assert ssh_data == "/tmp/snd-test-data"


def test_defaults_are_the_installed_linux_paths() -> None:
    env = dict(os.environ)
    env.pop("SND_DATA_DIR", None)
    env.pop("SND_LOG_DIR", None)
    main_data, main_log, ssh_data = _probe(env)
    assert main_data == "/var/lib/simple-network-dashboard"
    assert main_log == "/var/log/simple-network-dashboard"
    assert ssh_data == "/var/lib/simple-network-dashboard"
