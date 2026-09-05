"""Executable contracts for private runtime storage and file permissions."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess

import main
import pytest
import ssh_manager


ROOT = Path(__file__).resolve().parents[1]


def _find_bash() -> str | None:
    if os.name == "nt":
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        if git_bash.exists():
            return str(git_bash)
    return shutil.which("bash")


BASH = _find_bash()


def _run_installer_contract(body: str) -> subprocess.CompletedProcess[str]:
    if BASH is None:
        pytest.skip("Bash is required to exercise installer storage contracts")
    script = f"""
source ./install.sh
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT
APP_DIR="$TEST_ROOT/app"
DATA_DIR="$TEST_ROOT/data"
LOG_DIR="$TEST_ROOT/log"
chown() {{ :; }}
python3() {{ :; }}
{body}
"""
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_uninstaller_contract(body: str) -> subprocess.CompletedProcess[str]:
    if BASH is None:
        pytest.skip("Bash is required to exercise uninstaller storage contracts")
    script = f"""
source ./uninstall.sh
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT
DATA_DIR="$TEST_ROOT/data"
BACKUP_DIR="$TEST_ROOT/backup"
{body}
"""
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_fresh_install_does_not_import_legacy_runtime_files() -> None:
    result = _run_installer_contract(
        """
mkdir -p "$APP_DIR"
printf legacy > "$APP_DIR/devices.json"
printf legacy > "$APP_DIR/known_hosts"
ACCOUNT_STATE=fresh
repair_runtime_storage
printf 'devices=%s known_hosts=%s\\n' \
  "$(test -e "$DATA_DIR/devices.json" && echo yes || echo no)" \
  "$(test -e "$DATA_DIR/known_hosts" && echo yes || echo no)"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "devices=no known_hosts=no" in result.stdout


def test_update_migrates_legacy_files_without_overwriting_destination() -> None:
    result = _run_installer_contract(
        """
mkdir -p "$APP_DIR" "$DATA_DIR"
printf legacy-devices > "$APP_DIR/devices.json"
printf legacy-hosts > "$APP_DIR/known_hosts"
printf existing > "$DATA_DIR/devices.json"
ACCOUNT_STATE=managed-update
repair_runtime_storage
printf 'devices=%s hosts=%s legacy_devices=%s\\n' \
  "$(cat "$DATA_DIR/devices.json")" "$(cat "$DATA_DIR/known_hosts")" \
  "$(test -e "$APP_DIR/devices.json" && echo yes || echo no)"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "devices=existing hosts=legacy-hosts legacy_devices=yes" in result.stdout


def test_installer_rejects_symlink_legacy_source_before_runtime_mutation() -> None:
    result = _run_installer_contract(
        """
mkdir -p "$APP_DIR"
printf legacy > "$APP_DIR/devices.json"
chown() { printf chown >> "$TEST_ROOT/mutations"; }
chmod() { printf chmod >> "$TEST_ROOT/mutations"; }
mv() { printf mv >> "$TEST_ROOT/mutations"; }
is_safe_runtime_file() {
  if [ "$1" = "$APP_DIR/devices.json" ]; then return 1; fi
  return 0
}
ACCOUNT_STATE=managed-update
if repair_runtime_storage; then exit 10; fi
printf 'data=%s mutations=%s\\n' \
  "$(test -e "$DATA_DIR" && echo yes || echo no)" \
  "$(test -e "$TEST_ROOT/mutations" && cat "$TEST_ROOT/mutations" || echo none)"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "legacy runtime file" in result.stdout
    assert "data=no mutations=none" in result.stdout


def test_installer_rejects_symlink_destination_before_runtime_mutation() -> None:
    result = _run_installer_contract(
        """
mkdir -p "$DATA_DIR"
printf destination > "$DATA_DIR/devices.json"
chown() { printf chown >> "$TEST_ROOT/mutations"; }
chmod() { printf chmod >> "$TEST_ROOT/mutations"; }
mv() { printf mv >> "$TEST_ROOT/mutations"; }
is_safe_runtime_file() {
  if [ "$1" = "$DATA_DIR/devices.json" ]; then return 1; fi
  return 0
}
ACCOUNT_STATE=managed-update
if repair_runtime_storage; then exit 10; fi
printf 'mutations=%s\\n' "$(test -e "$TEST_ROOT/mutations" && cat "$TEST_ROOT/mutations" || echo none)"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "private runtime file" in result.stdout
    assert "mutations=none" in result.stdout


def test_installer_writes_restrictive_systemd_mask() -> None:
    assert "UMask=0077" in (ROOT / "install.sh").read_text(encoding="utf-8")


def test_installer_repairs_metadata_through_non_dereferencing_descriptors() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "os.O_NOFOLLOW" in installer
    assert 'mv -nT -- "$APP_DIR/$legacy_file" "$destination"' in installer
    assert "os.fchown(fd, uid, gid)" in installer
    assert "os.fchmod(fd, 0o600)" in installer
    assert 'chown snd:snd "$DATA_DIR"' not in installer


def test_devices_file_is_private_when_created_or_repaired(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(main.os, "open", lambda *args: calls.append(args) or 9)
    monkeypatch.setattr(main.os, "fchmod", lambda *args: calls.append(args))
    monkeypatch.setattr(main.os, "fdopen", lambda *_args, **_kwargs: _TextFile())

    assert main._save([])
    assert calls[0] == (main.DEVICES_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    assert calls[1] == (9, 0o600)


def test_debug_log_is_created_in_private_log_directory(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(main.os, "open", lambda *args: calls.append(args) or 9)
    monkeypatch.setattr(main.os, "fchmod", lambda *args: calls.append(args))
    monkeypatch.setattr(main.os, "fdopen", lambda *_args, **_kwargs: _TextFile())
    monkeypatch.setattr(main, "_debug_file", None)

    response = asyncio.run(main.toggle_debug(main.DebugIn(enabled=True)))
    assert response["path"].startswith(main.LOG_DIR)
    assert calls[0][2] == 0o600
    assert calls[1] == (9, 0o600)

    asyncio.run(main.toggle_debug(main.DebugIn(enabled=False)))


@pytest.mark.parametrize("operation", ["devices", "debug"])
@pytest.mark.parametrize("failure", ["fchmod", "fdopen"])
def test_private_file_creation_closes_raw_fd_on_setup_failure(monkeypatch, operation, failure) -> None:
    calls = []
    monkeypatch.setattr(main.os, "open", lambda *_args: 9)
    monkeypatch.setattr(main.os, "close", lambda fd: calls.append(("close", fd)))
    if failure == "fchmod":
        monkeypatch.setattr(main.os, "fchmod", lambda *_args: (_ for _ in ()).throw(OSError("denied")))
    else:
        monkeypatch.setattr(main.os, "fchmod", lambda *_args: None)
        monkeypatch.setattr(main.os, "fdopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("open failed")))

    if operation == "devices":
        assert not main._save([])
    else:
        monkeypatch.setattr(main, "_debug_file", None)
        with pytest.raises(OSError):
            asyncio.run(main.toggle_debug(main.DebugIn(enabled=True)))

    assert calls == [("close", 9)]


def test_known_hosts_file_is_private_when_saved(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(ssh_manager.os, "chmod", lambda *args: calls.append(args))

    class HostKeys:
        def save(self, path: str) -> None:
            calls.append(("save", path))

    ssh_manager._save_known_hosts(HostKeys())

    assert calls == [("save", ssh_manager.KNOWN_HOSTS_FILE), (ssh_manager.KNOWN_HOSTS_FILE, 0o600)]


class _TextFile:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def write(self, _text: str) -> int:
        return 0

    def close(self) -> None:
        pass


def test_uninstaller_references_private_data_and_log_directories() -> None:
    uninstaller = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    assert 'DATA_DIR="/var/lib/simple-network-dashboard"' in uninstaller
    assert 'LOG_DIR="/var/log/simple-network-dashboard"' in uninstaller
    assert 'cp --no-dereference -- "$DATA_DIR/devices.json" "$BACKUP_DIR/"' in uninstaller
    assert 'chown -R --no-dereference --' in uninstaller
    assert 'rm -rf "$DATA_DIR"' in uninstaller
    assert 'rm -rf "$LOG_DIR"' in uninstaller


def test_uninstaller_ignores_symlinked_private_config() -> None:
    result = _run_uninstaller_contract(
        """
mkdir -p "$DATA_DIR" "$BACKUP_DIR"
printf config > "$DATA_DIR/devices.json"
printf hosts > "$DATA_DIR/known_hosts"
is_private_config_file() {
  return 1
}
if has_private_config; then exit 10; fi
backup_private_config
printf 'backup_devices=%s backup_hosts=%s\\n' \
  "$(test -e "$BACKUP_DIR/devices.json" && echo yes || echo no)" \
  "$(test -e "$BACKUP_DIR/known_hosts" && echo yes || echo no)"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "backup_devices=no backup_hosts=no" in result.stdout


@pytest.mark.skipif(
    os.name == "nt",
    reason="the Windows sandbox cannot create POSIX symlinks for this shell contract",
)
@pytest.mark.parametrize("unsafe_path", ["$APP_DIR/devices.json", "$DATA_DIR/devices.json"])
def test_installer_rejects_real_symlinked_runtime_paths(unsafe_path: str) -> None:
    result = _run_installer_contract(
        f"""
mkdir -p "$APP_DIR" "$DATA_DIR"
printf target > "$TEST_ROOT/target"
ln -s "$TEST_ROOT/target" {unsafe_path}
ACCOUNT_STATE=managed-update
if repair_runtime_storage; then exit 10; fi
"""
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(
    os.name == "nt",
    reason="the Windows sandbox cannot create POSIX symlinks for this shell contract",
)
def test_uninstaller_ignores_real_symlinked_private_config() -> None:
    result = _run_uninstaller_contract(
        """
mkdir -p "$DATA_DIR" "$BACKUP_DIR"
printf target > "$TEST_ROOT/target"
ln -s "$TEST_ROOT/target" "$DATA_DIR/devices.json"
if has_private_config; then exit 10; fi
backup_private_config
test ! -e "$BACKUP_DIR/devices.json"
"""
    )

    assert result.returncode == 0, result.stderr
