"""Executable contracts for private runtime storage and file permissions."""

from __future__ import annotations

import asyncio
import builtins
import json
import os
from pathlib import Path
import shutil
import stat
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


def test_devices_save_uses_private_temp_file_and_atomic_replace(monkeypatch) -> None:
    events = []
    temp_path = os.path.join(main.DATA_DIR, ".devices-test.tmp")
    file = _AtomicTextFile(events, 11)
    monkeypatch.setattr(
        main.tempfile,
        "mkstemp",
        lambda **kwargs: events.append(("mkstemp", kwargs)) or (11, temp_path),
    )
    monkeypatch.setattr(main.os, "fchmod", lambda fd, mode: events.append(("fchmod", fd, mode)))
    monkeypatch.setattr(main.os, "fdopen", lambda fd, *_args, **_kwargs: events.append(("fdopen", fd)) or file)
    monkeypatch.setattr(main.os, "fsync", lambda fd: events.append(("fsync", fd)))
    monkeypatch.setattr(main.os, "replace", lambda source, target: events.append(("replace", source, target)))
    monkeypatch.setattr(main.os, "open", lambda path, flags: events.append(("open", path, flags)) or 12)
    monkeypatch.setattr(main.os, "close", lambda fd: events.append(("close", fd)))
    monkeypatch.setattr(main.os.path, "exists", lambda _path: False)
    monkeypatch.setattr(main, "_devices_cache", ["old"])

    devices = [{"id": "new"}]
    assert main._save(devices)
    assert events == [
        ("mkstemp", {"prefix": ".devices-", "suffix": ".tmp", "dir": main.DATA_DIR}),
        ("fchmod", 11, 0o600),
        ("fdopen", 11),
        ("flush",),
        ("fsync", 11),
        ("close-file",),
        ("replace", temp_path, main.DEVICES_FILE),
        ("open", main.DATA_DIR, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)),
        ("fsync", 12),
        ("close", 12),
        ("mkstemp", {"prefix": ".devices-", "suffix": ".tmp", "dir": main.DATA_DIR}),
        ("fchmod", 11, 0o600),
        ("fdopen", 11),
        ("flush",),
        ("fsync", 11),
        ("close-file",),
        ("replace", temp_path, f"{main.DEVICES_FILE}.bak"),
        ("open", main.DATA_DIR, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)),
        ("fsync", 12),
        ("close", 12),
    ]
    assert main._devices_cache == devices


def test_devices_save_durably_replaces_primary_before_backup(monkeypatch) -> None:
    events = []
    backup_temp = os.path.join(main.DATA_DIR, ".devices-backup.tmp")
    primary_temp = os.path.join(main.DATA_DIR, ".devices-primary.tmp")
    files = iter([
        (primary_temp, _AtomicTextFile(events, 21)),
        (backup_temp, _AtomicTextFile(events, 22)),
    ])
    monkeypatch.setattr(main, "_open_private_temp_file", lambda: next(files))
    monkeypatch.setattr(main.os, "fsync", lambda fd: events.append(("fsync", fd)))
    monkeypatch.setattr(main.os, "replace", lambda source, target: events.append(("replace", source, target)))
    monkeypatch.setattr(main, "_fsync_data_directory", lambda: events.append(("directory-fsync",)))

    assert main._save([{"id": "new"}])

    assert events == [
        ("flush",),
        ("fsync", 21),
        ("close-file",),
        ("replace", primary_temp, main.DEVICES_FILE),
        ("directory-fsync",),
        ("flush",),
        ("fsync", 22),
        ("close-file",),
        ("replace", backup_temp, f"{main.DEVICES_FILE}.bak"),
        ("directory-fsync",),
    ]


def test_backup_staging_failure_keeps_new_primary_and_previous_confirmed_backup(
    monkeypatch, tmp_path, capsys
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    devices_file = data_dir / "devices.json"
    monkeypatch.setattr(main, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(main, "DEVICES_FILE", str(devices_file))
    monkeypatch.setattr(main, "_fsync_data_directory", lambda: None)
    assert main._save([{"id": "confirmed"}])
    confirmed_backup = (data_dir / "devices.json.bak").read_text(encoding="utf-8")
    real_write = main._write_private_payload

    def fail_backup(path, contents, on_replaced=None):
        if path == f"{main.DEVICES_FILE}.bak":
            raise OSError("secret backup failure")
        return real_write(path, contents, on_replaced)

    monkeypatch.setattr(main, "_write_private_payload", fail_backup)
    new_devices = [{"id": "new"}]

    assert not main._save(new_devices)

    assert json.loads(devices_file.read_text(encoding="utf-8"))["devices"] == new_devices
    assert (data_dir / "devices.json.bak").read_text(encoding="utf-8") == confirmed_backup
    assert main._devices_cache == new_devices
    output = capsys.readouterr().out
    assert "durable mirrored save did not complete" in output
    assert "secret backup failure" not in output


def test_primary_pre_replacement_failure_preserves_confirmed_cache(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(main, "_devices_cache", [{"id": "confirmed"}])

    def fail_primary(path, contents, on_replaced=None):
        calls.append((path, contents))
        if path == main.DEVICES_FILE:
            raise OSError("primary staging failed")

    monkeypatch.setattr(main, "_write_private_payload", fail_primary)

    assert not main._save([{"id": "new"}])
    assert [path for path, _contents in calls] == [main.DEVICES_FILE]
    assert main._devices_cache == [{"id": "confirmed"}]


def test_primary_directory_fsync_failure_keeps_visible_replacement_in_cache(monkeypatch) -> None:
    monkeypatch.setattr(main, "_devices_cache", [{"id": "confirmed"}])

    def write_then_fail(path, _contents, on_replaced=None):
        if path == main.DEVICES_FILE:
            on_replaced()
            raise OSError("directory sync failed")

    monkeypatch.setattr(main, "_write_private_payload", write_then_fail)
    devices = [{"id": "new"}]

    assert not main._save(devices)
    assert main._devices_cache == devices


@pytest.mark.parametrize("failure", ["write", "flush", "file-fsync", "replace"])
@pytest.mark.parametrize("payload", ["primary", "backup"])
def test_payload_write_failures_preserve_the_last_durable_configuration(
    monkeypatch, tmp_path, capsys, failure, payload
) -> None:
    """Every pre-replacement payload failure leaves no staged file behind."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    devices_file = data_dir / "devices.json"
    backup_file = data_dir / "devices.json.bak"
    confirmed_devices = [{"id": "confirmed"}]
    new_devices = [{"id": "new"}]
    confirmed_contents = json.dumps({"_app": main.APP_NAME, "devices": confirmed_devices}, indent=2)
    devices_file.write_text(confirmed_contents, encoding="utf-8")
    backup_file.write_text(confirmed_contents, encoding="utf-8")
    monkeypatch.setattr(main, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(main, "DEVICES_FILE", str(devices_file))
    monkeypatch.setattr(main, "_fsync_data_directory", lambda: None)
    monkeypatch.setattr(main, "_devices_cache", confirmed_devices)

    real_open_temp = main._open_private_temp_file
    real_fsync = main.os.fsync
    real_replace = main.os.replace
    opened_payloads = 0

    def open_temp_with_failure():
        nonlocal opened_payloads
        temp_path, file = real_open_temp()
        opened_payloads += 1
        if (payload == "primary" and opened_payloads == 1) or (
            payload == "backup" and opened_payloads == 2
        ):
            return temp_path, _FailingAtomicTextFile(file, failure)
        return temp_path, file

    def fail_file_fsync(fd):
        if fd == _FailingAtomicTextFile.FILE_FSYNC_FD:
            raise OSError("private payload failure")
        return real_fsync(fd)

    def fail_replace(source, destination):
        is_primary = destination == str(devices_file)
        if failure == "replace" and is_primary == (payload == "primary"):
            raise OSError("private payload failure")
        return real_replace(source, destination)

    monkeypatch.setattr(main, "_open_private_temp_file", open_temp_with_failure)
    if failure == "file-fsync":
        monkeypatch.setattr(main.os, "fsync", fail_file_fsync)
    if failure == "replace":
        monkeypatch.setattr(main.os, "replace", fail_replace)

    assert not main._save(new_devices)

    output = capsys.readouterr().out
    if payload == "primary":
        assert devices_file.read_text(encoding="utf-8") == confirmed_contents
        assert backup_file.read_text(encoding="utf-8") == confirmed_contents
        assert main._devices_cache == confirmed_devices
        assert "SAVE FAILED" in output
    else:
        assert json.loads(devices_file.read_text(encoding="utf-8"))["devices"] == new_devices
        assert backup_file.read_text(encoding="utf-8") == confirmed_contents
        assert main._devices_cache == new_devices
        assert "durable mirrored save did not complete" in output
    assert list(data_dir.glob(".devices-*.tmp")) == []
    assert "private payload failure" not in output


@pytest.mark.parametrize("payload", ["primary", "backup"])
def test_payload_directory_fsync_failures_keep_visible_replacements(monkeypatch, tmp_path, capsys, payload) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    devices_file = data_dir / "devices.json"
    backup_file = data_dir / "devices.json.bak"
    confirmed_devices = [{"id": "confirmed"}]
    new_devices = [{"id": "new"}]
    confirmed_contents = json.dumps({"_app": main.APP_NAME, "devices": confirmed_devices}, indent=2)
    devices_file.write_text(confirmed_contents, encoding="utf-8")
    backup_file.write_text(confirmed_contents, encoding="utf-8")
    monkeypatch.setattr(main, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(main, "DEVICES_FILE", str(devices_file))
    monkeypatch.setattr(main, "_devices_cache", confirmed_devices)
    directory_syncs = 0

    def fail_selected_directory_sync():
        nonlocal directory_syncs
        directory_syncs += 1
        if directory_syncs == (1 if payload == "primary" else 2):
            raise OSError("private directory failure")

    monkeypatch.setattr(main, "_fsync_data_directory", fail_selected_directory_sync)

    assert not main._save(new_devices)

    assert json.loads(devices_file.read_text(encoding="utf-8"))["devices"] == new_devices
    if payload == "primary":
        assert backup_file.read_text(encoding="utf-8") == confirmed_contents
    else:
        assert json.loads(backup_file.read_text(encoding="utf-8"))["devices"] == new_devices
    assert main._devices_cache == new_devices
    assert list(data_dir.glob(".devices-*.tmp")) == []
    output = capsys.readouterr().out
    assert "durable mirrored save did not complete" in output
    assert "private directory failure" not in output


def test_first_devices_save_creates_matching_primary_and_backup(monkeypatch, tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    devices_file = data_dir / "devices.json"
    monkeypatch.setattr(main, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(main, "DEVICES_FILE", str(devices_file))
    monkeypatch.setattr(main, "_fsync_data_directory", lambda: None)

    devices = [{"id": "saved"}]
    assert main._save(devices)
    assert json.loads(devices_file.read_text(encoding="utf-8")) == {
        "_app": main.APP_NAME,
        "devices": devices,
    }
    assert (data_dir / "devices.json.bak").read_text(encoding="utf-8") == devices_file.read_text(encoding="utf-8")
    assert list(data_dir.glob(".devices-*.tmp")) == []
    if os.name != "nt":
        assert stat.S_IMODE(devices_file.stat().st_mode) == 0o600


def test_devices_save_mirrors_exact_new_payload_to_private_backup(monkeypatch, tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    devices_file = data_dir / "devices.json"
    backup_file = data_dir / "devices.json.bak"
    previous_contents = '{\n  "devices": [{"id": "previous", "metrics_port": "9100"}]\n}\n'
    devices_file.write_text(previous_contents, encoding="utf-8")
    monkeypatch.setattr(main, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(main, "DEVICES_FILE", str(devices_file))
    monkeypatch.setattr(main, "_fsync_data_directory", lambda: None)

    assert main._save([{"id": "new"}])

    assert backup_file.read_text(encoding="utf-8") == devices_file.read_text(encoding="utf-8")
    assert json.loads(devices_file.read_text(encoding="utf-8"))["devices"] == [{"id": "new"}]
    if os.name != "nt":
        assert stat.S_IMODE(backup_file.stat().st_mode) == 0o600


def test_devices_save_keeps_backup_mirrored_on_later_saves(monkeypatch, tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    devices_file = data_dir / "devices.json"
    monkeypatch.setattr(main, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(main, "DEVICES_FILE", str(devices_file))
    monkeypatch.setattr(main, "_fsync_data_directory", lambda: None)

    assert main._save([{"id": "first"}])
    assert (data_dir / "devices.json.bak").read_text(encoding="utf-8") == devices_file.read_text(encoding="utf-8")
    assert main._save([{"id": "second"}])
    assert (data_dir / "devices.json.bak").read_text(encoding="utf-8") == devices_file.read_text(encoding="utf-8")
    assert main._save([{"id": "third"}])
    assert (data_dir / "devices.json.bak").read_text(encoding="utf-8") == devices_file.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "corrupt_primary",
    ["{invalid", '{"devices": [{"metrics_port": "not-a-number"}]}'],
    ids=["unparseable", "cannot-normalize"],
)
def test_devices_save_replaces_backup_with_new_payload_when_primary_is_corrupt(monkeypatch, tmp_path, corrupt_primary) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    devices_file = data_dir / "devices.json"
    backup_file = data_dir / "devices.json.bak"
    devices_file.write_text(corrupt_primary, encoding="utf-8")
    backup_file.write_text("known good backup", encoding="utf-8")
    monkeypatch.setattr(main, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(main, "DEVICES_FILE", str(devices_file))
    monkeypatch.setattr(main, "_fsync_data_directory", lambda: None)

    assert main._save([{"id": "new"}])

    assert backup_file.read_text(encoding="utf-8") == devices_file.read_text(encoding="utf-8")
    assert json.loads(devices_file.read_text(encoding="utf-8"))["devices"] == [{"id": "new"}]


def test_devices_save_replaces_backup_with_new_payload_when_primary_is_unreadable(monkeypatch, tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    devices_file = data_dir / "devices.json"
    backup_file = data_dir / "devices.json.bak"
    devices_file.write_text('{"devices": [{"id": "previous"}]}', encoding="utf-8")
    backup_file.write_text("known good backup", encoding="utf-8")
    monkeypatch.setattr(main, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(main, "DEVICES_FILE", str(devices_file))
    monkeypatch.setattr(main, "_fsync_data_directory", lambda: None)
    real_open = builtins.open

    def deny_primary_read(path, mode="r", *args, **kwargs):
        if os.fspath(path) == str(devices_file) and mode == "r":
            raise PermissionError("denied")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", deny_primary_read)

    assert main._save([{"id": "new"}])

    assert backup_file.read_text(encoding="utf-8") == devices_file.read_text(encoding="utf-8")
    assert json.loads(devices_file.read_text(encoding="utf-8"))["devices"] == [{"id": "new"}]


@pytest.mark.parametrize(
    "primary_contents",
    [None, "{invalid", '{"devices": [{"metrics_port": "not-a-number"}]}'],
    ids=["missing", "unparseable", "cannot-normalize"],
)
def test_startup_recovers_valid_backup(
    monkeypatch, tmp_path, capsys, primary_contents
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    devices_file = data_dir / "devices.json"
    backup_file = data_dir / "devices.json.bak"
    if primary_contents is not None:
        devices_file.write_text(primary_contents, encoding="utf-8")
    backup_file.write_text('{"devices": [{"id": "from-backup"}]}', encoding="utf-8")
    monkeypatch.setattr(main, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(main, "DEVICES_FILE", str(devices_file))
    monkeypatch.setattr(main, "_fsync_data_directory", lambda: None)

    devices, recovered, read_only = main._load_startup_devices()

    assert devices == [{"id": "from-backup", "commands": [], "metrics_port": 9100}]
    assert recovered is True
    assert read_only is False
    assert main._recovery_backup_contents is None
    assert devices_file.read_text(encoding="utf-8") == backup_file.read_text(encoding="utf-8")
    assert "from-backup" not in capsys.readouterr().out


def test_fresh_startup_without_configuration_is_empty_and_writable(monkeypatch, tmp_path) -> None:
    devices_file = tmp_path / "devices.json"
    monkeypatch.setattr(main, "DEVICES_FILE", str(devices_file))

    devices, recovered, read_only = main._load_startup_devices()

    assert devices == []
    assert recovered is False
    assert read_only is False
    assert main._recovery_backup_contents is None


@pytest.mark.parametrize(
    ("primary_contents", "backup_contents"),
    [
        ("{invalid", None),
        (None, "{invalid"),
        ("{invalid", "{also-invalid"),
    ],
    ids=["invalid-primary", "invalid-backup", "both-invalid"],
)
def test_startup_without_any_valid_configuration_is_empty_and_read_only(
    monkeypatch, tmp_path, capsys, primary_contents, backup_contents
) -> None:
    devices_file = tmp_path / "devices.json"
    backup_file = tmp_path / "devices.json.bak"
    if primary_contents is not None:
        devices_file.write_text(primary_contents, encoding="utf-8")
    if backup_contents is not None:
        backup_file.write_text(backup_contents, encoding="utf-8")
    monkeypatch.setattr(main, "DEVICES_FILE", str(devices_file))

    devices, recovered, read_only = main._load_startup_devices()

    assert devices == []
    assert recovered is False
    assert read_only is True
    assert main._recovery_backup_contents is None
    output = capsys.readouterr().out
    assert "Configuration recovery mode" in output
    for private_contents in (primary_contents, backup_contents):
        if private_contents is not None:
            assert private_contents not in output


def test_lifespan_latches_recovery_mode_until_restart(monkeypatch) -> None:
    cached = [{"id": "from-backup", "commands": [], "metrics_port": 9100}]
    monkeypatch.setattr(main, "_recovery_mode", False)
    monkeypatch.setattr(main, "_storage_warning", False)
    monkeypatch.setattr(main, "_load_startup_devices", lambda: (cached, False, True))
    monkeypatch.setattr(main.os.path, "exists", lambda _path: False)

    async def check_latched_state() -> None:
        async with main.lifespan(main.app):
            assert main._devices_cache == cached
            assert main._recovery_mode is True
            assert main._storage_warning is True

    asyncio.run(check_latched_state())


def test_lifespan_sets_recovered_notice_without_read_only_mode(monkeypatch) -> None:
    cached = [{"id": "from-backup", "commands": [], "metrics_port": 9100}]
    monkeypatch.setattr(main, "_recovery_mode", False)
    monkeypatch.setattr(main, "_storage_warning", False)
    monkeypatch.setattr(main, "_recovered_notice", False)
    monkeypatch.setattr(main, "_load_startup_devices", lambda: (cached, True, False))
    monkeypatch.setattr(main.os.path, "exists", lambda _path: False)

    async def check_notice_state() -> None:
        async with main.lifespan(main.app):
            assert main._devices_cache == cached
            assert main._recovered_notice is True
            assert main._recovery_mode is False
            assert main._storage_warning is False

    asyncio.run(check_notice_state())


def test_startup_recovery_hides_primary_read_error(monkeypatch, tmp_path, capsys) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    devices_file = data_dir / "devices.json"
    backup_file = data_dir / "devices.json.bak"
    devices_file.write_text('{"devices": []}', encoding="utf-8")
    backup_file.write_text('{"devices": [{"id": "from-backup"}]}', encoding="utf-8")
    monkeypatch.setattr(main, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(main, "DEVICES_FILE", str(devices_file))
    monkeypatch.setattr(main, "_fsync_data_directory", lambda: None)
    real_open = builtins.open

    def deny_primary_read(path, mode="r", *args, **kwargs):
        if os.fspath(path) == str(devices_file) and mode == "r":
            raise PermissionError("private primary detail")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", deny_primary_read)

    devices, recovered, read_only = main._load_startup_devices()

    assert devices[0]["id"] == "from-backup"
    assert recovered is False
    assert read_only is True
    assert main._recovery_backup_contents == backup_file.read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert "Configuration recovery mode" in output
    assert "private primary detail" not in output
    assert "from-backup" not in output


def test_startup_recovery_replacement_failure_latches_read_only_mode(monkeypatch, tmp_path, capsys) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    devices_file = data_dir / "devices.json"
    backup_file = data_dir / "devices.json.bak"
    corrupt_primary = "{invalid"
    backup_contents = '{"devices": [{"id": "from-backup"}]}'
    devices_file.write_text(corrupt_primary, encoding="utf-8")
    backup_file.write_text(backup_contents, encoding="utf-8")
    monkeypatch.setattr(main, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(main, "DEVICES_FILE", str(devices_file))
    monkeypatch.setattr(
        main.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("private replacement failure")),
    )

    devices, recovered, read_only = main._load_startup_devices()

    assert devices == [{"id": "from-backup", "commands": [], "metrics_port": 9100}]
    assert recovered is False
    assert read_only is True
    assert main._recovery_backup_contents == backup_contents
    assert devices_file.read_text(encoding="utf-8") == corrupt_primary
    assert backup_file.read_text(encoding="utf-8") == backup_contents
    assert list(data_dir.glob(".devices-*.tmp")) == []
    output = capsys.readouterr().out
    assert "Configuration recovery mode" in output
    assert "private replacement failure" not in output
    assert "from-backup" not in output


def test_recovery_mode_uses_cached_devices_and_rejects_all_configuration_mutations(monkeypatch) -> None:
    cached = [{"id": "saved", "name": "Saved", "commands": [], "metrics_port": 9100}]
    monkeypatch.setattr(main, "_devices_cache", cached)
    monkeypatch.setattr(main, "_storage_warning", True)
    monkeypatch.setattr(main, "_recovery_mode", True)
    monkeypatch.setattr(main, "_save", lambda _devices: pytest.fail("attempted save in recovery mode"))
    monkeypatch.setattr(main.ssh_mgr, "disconnect", lambda _device_id: pytest.fail("disconnected during rejected mutation"))
    monkeypatch.setattr(main.ws_mgr, "broadcast", lambda _message: pytest.fail("broadcast rejected mutation"))
    monkeypatch.setattr(main, "_selected_device_id", "saved")

    assert asyncio.run(main.get_devices()) == cached
    add_result = asyncio.run(main.upsert_device(main.DeviceIn(name="New", host="new", username="user")))
    delete_result = asyncio.run(main.delete_device("saved"))
    command_result = asyncio.run(main.update_commands("saved", main.CommandsIn(commands=[])))

    assert add_result == {"ok": False, "error": main._RECOVERY_READ_ONLY_ERROR}
    assert delete_result == {"ok": False, "error": main._RECOVERY_READ_ONLY_ERROR}
    assert command_result == {"ok": False, "error": main._RECOVERY_READ_ONLY_ERROR}
    assert main._selected_device_id == "saved"


def test_retry_recovery_restores_retained_validated_payload_and_broadcasts(monkeypatch) -> None:
    payload = '{"devices": [{"id": "from-backup"}]}'
    restored = []
    broadcasts = []
    monkeypatch.setattr(main, "_recovery_mode", True)
    monkeypatch.setattr(main, "_storage_warning", True)
    monkeypatch.setattr(main, "_recovery_backup_contents", payload)
    monkeypatch.setattr(main, "_restore_and_verify_primary", lambda contents: restored.append(contents))

    async def record_broadcast(message):
        broadcasts.append(message)

    monkeypatch.setattr(main.ws_mgr, "broadcast", record_broadcast)

    result = asyncio.run(main.retry_recovery())

    assert result == {"ok": True, "recovered": True}
    assert restored == [payload]
    assert main._recovery_mode is False
    assert main._storage_warning is False
    assert main._recovery_backup_contents is None
    assert broadcasts == [{"type": "recovery_restored"}]


def test_retry_recovery_failure_keeps_payload_and_hides_raw_error(monkeypatch, capsys) -> None:
    payload = '{"devices": [{"id": "from-backup"}]}'
    monkeypatch.setattr(main, "_recovery_mode", True)
    monkeypatch.setattr(main, "_recovery_backup_contents", payload)
    monkeypatch.setattr(
        main,
        "_restore_and_verify_primary",
        lambda _contents: (_ for _ in ()).throw(OSError("private failure detail")),
    )

    result = asyncio.run(main.retry_recovery())

    assert result == {"ok": False, "error": "Recovery could not finish. Your saved data remains protected."}
    assert main._recovery_mode is True
    assert main._recovery_backup_contents == payload
    output = capsys.readouterr().out
    assert "OSError" in output
    assert "private failure detail" not in output
    assert "from-backup" not in output


def test_retry_recovery_is_serialized_and_idempotent_after_success(monkeypatch) -> None:
    payload = '{"devices": [{"id": "from-backup"}]}'
    calls = []
    broadcasts = []
    monkeypatch.setattr(main, "_recovery_mode", True)
    monkeypatch.setattr(main, "_recovery_backup_contents", payload)
    monkeypatch.setattr(main, "_restore_and_verify_primary", lambda contents: calls.append(contents))

    async def record_broadcast(message):
        broadcasts.append(message)

    monkeypatch.setattr(main.ws_mgr, "broadcast", record_broadcast)

    async def retry_twice():
        return await asyncio.gather(main.retry_recovery(), main.retry_recovery())

    assert asyncio.run(retry_twice()) == [
        {"ok": True, "recovered": True},
        {"ok": True, "recovered": False},
    ]
    assert calls == [payload]
    assert broadcasts == [{"type": "recovery_restored"}]


def test_retry_recovery_is_safe_when_no_recovery_is_active(monkeypatch) -> None:
    monkeypatch.setattr(main, "_recovery_mode", False)

    assert asyncio.run(main.retry_recovery()) == {"ok": True, "recovered": False}


def test_save_defensively_rejects_recovery_mode_without_touching_storage(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "_recovery_mode", True)
    monkeypatch.setattr(main, "_open_private_temp_file", lambda: pytest.fail("opened storage in recovery mode"))

    assert not main._save([{"id": "replacement"}])
    assert "read-only until recovery succeeds" in capsys.readouterr().out


def test_failed_command_save_does_not_mutate_cached_configuration(monkeypatch) -> None:
    cached = [{"id": "saved", "commands": [{"name": "Original", "command": "uptime"}]}]
    monkeypatch.setattr(main, "_devices_cache", cached)
    monkeypatch.setattr(main, "_recovery_mode", False)
    monkeypatch.setattr(main, "_save", lambda _devices: False)

    result = asyncio.run(
        main.update_commands(
            "saved",
            main.CommandsIn(commands=[{"name": "Replacement", "command": "hostname"}]),
        )
    )

    assert result == {"ok": False, "error": main._SAVE_ERROR}
    assert main._devices_cache == cached
    assert main._devices_cache[0]["commands"][0]["name"] == "Original"


def test_recovery_ui_shows_only_simple_warning_and_restored_notice() -> None:
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert 'id="recoveryBanner"' in ui
    assert "classList.toggle('show', !!msg.recovery_mode)" in ui
    assert "CONFIG_READ_ONLY = !!msg.recovery_mode" in ui
    assert "$('addBtn').disabled = CONFIG_READ_ONLY" in ui
    assert "$('addCmdBtn').disabled = !d || CONFIG_READ_ONLY" in ui
    assert "msg.recovered_notice" in ui
    assert "Configuration restored from backup" in ui
    assert "notice-banner" in ui
    assert "RECOVERY_NOTICE_SHOWN" in ui
    assert 'id="retryRecovery"' in ui
    assert 'id="recoveryActions"' in ui
    assert 'id="recoveryRetryStatus" aria-live="polite"' in ui
    assert "msg.recovery_retry_available" in ui
    assert "POST('/api/retry-recovery', {})" in ui
    assert "button.textContent = 'Trying…'" in ui
    assert "case 'recovery_restored':" in ui
    assert "handleRecoveryRestored()" in ui
    assert "Data remains protected" in ui
    assert "recoveryModal" not in ui
    assert "recoverySteps" not in ui
    assert "Copy recovery command" not in ui


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
    if operation == "devices":
        monkeypatch.setattr(
            main.tempfile,
            "mkstemp",
            lambda **_kwargs: (9, os.path.join(main.DATA_DIR, ".devices-test.tmp")),
        )
        monkeypatch.setattr(main.os.path, "exists", lambda _path: False)
    else:
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
        with pytest.raises(OSError):
            main._open_private_file(os.path.join(main.LOG_DIR, "Debug_Log_test.txt"))

    assert calls == [("close", 9)]


def test_private_temp_creation_attempts_cleanup_when_raw_fd_close_fails(monkeypatch) -> None:
    events = []
    temp_path = os.path.join(main.DATA_DIR, ".devices-test.tmp")
    monkeypatch.setattr(main.tempfile, "mkstemp", lambda **_kwargs: (9, temp_path))
    monkeypatch.setattr(
        main.os,
        "fchmod",
        lambda *_args: (_ for _ in ()).throw(OSError("fchmod denied")),
    )

    def fail_close(fd):
        events.append(("close", fd))
        raise OSError("close failed")

    monkeypatch.setattr(main.os, "close", fail_close)
    monkeypatch.setattr(main.os, "unlink", lambda path: events.append(("unlink", path)))

    with pytest.raises(OSError, match="fchmod denied"):
        main._open_private_temp_file()

    assert events == [("close", 9), ("unlink", temp_path)]


def test_devices_save_serialization_failure_does_not_touch_storage(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        main.json,
        "dumps",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("secret configuration")),
    )
    monkeypatch.setattr(main, "_open_private_temp_file", lambda: pytest.fail("opened storage"))
    monkeypatch.setattr(main, "_devices_cache", ["old"])

    assert not main._save([{"id": "new"}])
    assert main._devices_cache == ["old"]
    assert "secret configuration" not in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["file-fsync", "replace"])
def test_devices_save_cleans_up_temp_file_before_replacement(monkeypatch, capsys, failure) -> None:
    events = []
    temp_path = os.path.join(main.DATA_DIR, ".devices-test.tmp")
    file = _AtomicTextFile(events, 11)
    monkeypatch.setattr(main, "_open_private_temp_file", lambda: (temp_path, file))
    monkeypatch.setattr(main.os, "unlink", lambda path: events.append(("unlink", path)))
    monkeypatch.setattr(main, "_devices_cache", ["old"])
    if failure == "file-fsync":
        monkeypatch.setattr(
            main.os,
            "fsync",
            lambda _fd: (_ for _ in ()).throw(OSError("secret configuration")),
        )
    else:
        monkeypatch.setattr(
            main.os,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("secret configuration")),
        )

    assert not main._save([{"id": "new"}])
    assert events[-1] == ("unlink", temp_path)
    assert main._devices_cache == ["old"]
    assert "secret configuration" not in capsys.readouterr().out


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


class _AtomicTextFile(_TextFile):
    def __init__(self, events, fd: int):
        self.events = events
        self.fd = fd

    def flush(self) -> None:
        self.events.append(("flush",))

    def fileno(self) -> int:
        return self.fd

    def __exit__(self, *_args):
        self.events.append(("close-file",))
        return False


class _FailingAtomicTextFile:
    FILE_FSYNC_FD = -999

    def __init__(self, file, failure: str):
        self.file = file
        self.failure = failure

    def __enter__(self):
        self.file.__enter__()
        return self

    def __exit__(self, *args):
        return self.file.__exit__(*args)

    def write(self, contents: str) -> int:
        if self.failure == "write":
            raise OSError("private payload failure")
        return self.file.write(contents)

    def flush(self) -> None:
        if self.failure == "flush":
            raise OSError("private payload failure")
        self.file.flush()

    def fileno(self) -> int:
        if self.failure == "file-fsync":
            return self.FILE_FSYNC_FD
        return self.file.fileno()


def test_uninstaller_references_private_data_and_log_directories() -> None:
    uninstaller = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    assert 'DATA_DIR="/var/lib/simple-network-dashboard"' in uninstaller
    assert 'LOG_DIR="/var/log/simple-network-dashboard"' in uninstaller
    assert 'create_private_backup_dir()' in uninstaller
    assert 'mkdir -m 0700 -- "$BACKUP_DIR"' in uninstaller
    assert 'chmod 0600 -- "$backup_file"' in uninstaller
    assert 'finalize_private_backup_dir()' in uninstaller
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


def test_uninstaller_creates_private_backup_for_invoking_user() -> None:
    result = _run_uninstaller_contract(
        """
mkdir -p "$DATA_DIR"
printf config > "$DATA_DIR/devices.json"
printf hosts > "$DATA_DIR/known_hosts"
BACKUP_OWNER=deploy
BACKUP_GROUP=deploy
mkdir() { command mkdir "$@"; printf 'mkdir:%s\\n' "$*" >> "$TEST_ROOT/metadata-calls"; }
chown() { printf 'chown:%s\\n' "$*" >> "$TEST_ROOT/metadata-calls"; }
chmod() { printf 'chmod:%s\\n' "$*" >> "$TEST_ROOT/metadata-calls"; }
if ! create_private_backup_dir; then exit 10; fi
if ! backup_private_config; then exit 11; fi
if ! finalize_private_backup_dir; then exit 12; fi
cat "$TEST_ROOT/metadata-calls"
"""
    )

    assert result.returncode == 0, result.stderr
    calls = result.stdout.splitlines()
    assert calls[0].startswith("mkdir:-m 0700 --")
    assert any(call.startswith("chmod:0600 --") for call in calls)
    file_chowns = [
        index
        for index, call in enumerate(calls)
        if call.startswith("chown:--no-dereference -- deploy:deploy")
    ]
    directory_chown = next(
        index
        for index, call in enumerate(calls)
        if call.startswith("chown:-- deploy:deploy")
    )
    assert file_chowns
    assert all(calls[index - 1].startswith("chmod:0600 --") for index in file_chowns)
    assert all(index < directory_chown for index in file_chowns)


def test_uninstaller_refuses_to_continue_after_unsafe_backup_failure() -> None:
    result = _run_uninstaller_contract(
        """
mkdir -p "$DATA_DIR" "$BACKUP_DIR"
printf config > "$DATA_DIR/devices.json"
BACKUP_OWNER=deploy
BACKUP_GROUP=deploy
cp() { return 1; }
if backup_private_config; then exit 10; fi
"""
    )

    assert result.returncode == 0, result.stderr
    assert "could not copy private configuration" in result.stdout


def test_uninstaller_main_stops_before_removal_when_backup_copy_fails() -> None:
    result = _run_uninstaller_contract(
        """
APP_DIR="$TEST_ROOT/app"
LOG_DIR="$TEST_ROOT/log"
INSTALL_STATE="$TEST_ROOT/install-state"
SUDO_USER=$(id -un)
mkdir -p "$DATA_DIR"
printf config > "$DATA_DIR/devices.json"
id() {
  if [ "$1" = -gn ] && [ "$2" = "$SUDO_USER" ]; then printf test-group; return 0; fi
  command id "$@"
}
require_root() { :; }
create_private_backup_dir() { :; }
cp() { return 1; }
pwd() { printf '%s\\n' "$TEST_ROOT"; }
date() { printf fixed; }
systemctl() { printf systemctl > "$TEST_ROOT/removal-called"; }
rm() { printf rm > "$TEST_ROOT/removal-called"; }
userdel() { printf userdel > "$TEST_ROOT/removal-called"; }
groupdel() { printf groupdel > "$TEST_ROOT/removal-called"; }
if (main --yes); then exit 10; fi
printf 'removal=%s\\n' "$(test -e "$TEST_ROOT/removal-called" && cat "$TEST_ROOT/removal-called" || echo no)"
unset -f rm
"""
    )

    assert result.returncode == 0, result.stderr
    assert "Nothing was removed" in result.stdout
    assert "removal=no" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX ownership and mode semantics")
def test_uninstaller_finalizes_private_backup_metadata_on_posix() -> None:
    result = _run_uninstaller_contract(
        """
BACKUP_OWNER=$(id -un)
BACKUP_GROUP=$(id -gn "$BACKUP_OWNER")
mkdir -p "$DATA_DIR"
printf config > "$DATA_DIR/devices.json"
printf hosts > "$DATA_DIR/known_hosts"
create_private_backup_dir
backup_private_config
finalize_private_backup_dir
printf 'directory=%s devices=%s hosts=%s\\n' \\
  "$(stat -c '%u:%g:%a' "$BACKUP_DIR")" \\
  "$(stat -c '%u:%g:%a' "$BACKUP_DIR/devices.json")" \\
  "$(stat -c '%u:%g:%a' "$BACKUP_DIR/known_hosts")"
"""
    )

    assert result.returncode == 0, result.stderr
    uid_gid = f"{os.getuid()}:{os.getgid()}"
    assert f"directory={uid_gid}:700" in result.stdout
    assert f"devices={uid_gid}:600" in result.stdout
    assert f"hosts={uid_gid}:600" in result.stdout


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
