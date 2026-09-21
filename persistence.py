import json
import os
import tempfile

from config import APP_NAME, DATA_DIR, DEVICES_FILE
import runtime_state
import debug_log


def _parse_devices(contents: str) -> list:
    """Parse and normalize a devices.json payload."""
    data = json.loads(contents)
    devices = data.get("devices", data) if isinstance(data, dict) else data
    return [_norm(d) for d in devices if isinstance(d, dict)]


def _load_startup_devices() -> tuple[list, bool, bool]:
    """Load the primary once, repairing it from a valid backup when possible.

    Returns devices, whether a repair succeeded, and whether failed repair left
    the process in restart-latched read-only mode.
    """
    runtime_state._recovery_backup_contents = None
    try:
        with open(DEVICES_FILE, "r", encoding="utf-8") as f:
            return _parse_devices(f.read()), False, False
    except Exception as primary_error:
        try:
            with open(f"{DEVICES_FILE}.bak", "r", encoding="utf-8") as f:
                backup_contents = f.read()
            devices = _parse_devices(backup_contents)
        except Exception as backup_error:
            if isinstance(primary_error, FileNotFoundError) and isinstance(
                backup_error, FileNotFoundError
            ):
                return [], False, False
            print(
                "WARNING: Configuration recovery mode is active. "
                "Neither configuration copy could be loaded; "
                "configuration changes are disabled to protect the existing files.",
                flush=True,
            )
            return [], False, True
        try:
            _replace_primary_from_backup(backup_contents)
            with open(DEVICES_FILE, "r", encoding="utf-8") as f:
                _parse_devices(f.read())
        except Exception as repair_error:
            runtime_state._recovery_backup_contents = backup_contents
            print(
                "WARNING: Configuration recovery mode is active. "
                "Automatic restoration from the validated backup did not complete "
                f"({type(repair_error).__name__}); "
                "configuration changes are disabled until recovery succeeds.",
                flush=True,
            )
            return devices, False, True
        print(
            "NOTICE: Configuration was restored automatically from its validated backup because "
            f"{_configuration_failure_reason(primary_error)}.",
            flush=True,
        )
        return devices, True, False


def _configuration_failure_reason(error: Exception) -> str:
    """Describe a primary-load failure without including file contents or raw errors."""
    if isinstance(error, FileNotFoundError):
        return "the primary configuration is missing"
    if isinstance(error, json.JSONDecodeError):
        return "the primary configuration contains invalid JSON"
    if isinstance(error, UnicodeError):
        return "the primary configuration is not valid UTF-8"
    if isinstance(error, OSError):
        return f"the primary configuration could not be read ({type(error).__name__})"
    return f"the primary configuration could not be normalized ({type(error).__name__})"


def _open_private_file(path: str, *, buffering: int = -1):
    fd = None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        file = os.fdopen(fd, "w", encoding="utf-8", buffering=buffering)
        fd = None
        return file
    except Exception:
        if fd is not None:
            os.close(fd)
        raise


def _open_private_temp_file() -> tuple[str, object]:
    """Create a private, same-directory file for an atomic device save."""
    fd = None
    path = None
    try:
        fd, path = tempfile.mkstemp(prefix=".devices-", suffix=".tmp", dir=DATA_DIR)
        os.fchmod(fd, 0o600)
        file = os.fdopen(fd, "w", encoding="utf-8")
        fd = None
        return path, file
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if path is not None:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise


def _fsync_data_directory() -> None:
    """Persist the replacement entry after the temporary file is replaced."""
    # Windows does not force-flush this directory entry, so a crash or power loss
    # can lose the just-replaced file's directory update during local development.
    if os.name == "nt":
        return
    fd = None
    try:
        fd = os.open(DATA_DIR, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(fd)
    finally:
        if fd is not None:
            os.close(fd)


def _write_private_payload(path: str, contents: str, on_replaced=None) -> None:
    """Durably replace one private configuration file with an exact payload."""
    temp_path = None
    try:
        temp_path, f = _open_private_temp_file()
        with f:
            f.write(contents)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
        temp_path = None
        if on_replaced is not None:
            on_replaced()
        _fsync_data_directory()
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _replace_primary_from_backup(contents: str) -> None:
    """Atomically restore the exact validated backup payload to the primary."""
    _write_private_payload(DEVICES_FILE, contents)


def _restore_and_verify_primary(contents: str) -> None:
    """Restore an already-validated payload and prove the replacement can load."""
    _replace_primary_from_backup(contents)
    with open(DEVICES_FILE, "r", encoding="utf-8") as f:
        _parse_devices(f.read())


def _save(devices: list) -> bool:
    if runtime_state._recovery_mode:
        msg = "SAVE REJECTED: configuration recovery mode is read-only until recovery succeeds."
        debug_log._debug_write(msg)
        print(msg, flush=True)
        return False
    replaced = False

    def primary_replaced() -> None:
        nonlocal replaced
        replaced = True
        runtime_state._devices_cache = list(devices)

    try:
        contents = json.dumps({"_app": APP_NAME, "devices": devices}, indent=2)
        _parse_devices(contents)
        _write_private_payload(DEVICES_FILE, contents, primary_replaced)
        _write_private_payload(f"{DEVICES_FILE}.bak", contents)
        return True
    except Exception:
        if replaced:
            msg = "SAVE UNCONFIRMED: primary configuration was replaced but the durable mirrored save did not complete."
        else:
            msg = "SAVE FAILED: could not persist device configuration."
        debug_log._debug_write(msg)
        print(msg, flush=True)  # always visible in the systemd journal, even with debug logging off
        return False


def _norm(d: dict) -> dict:
    cmds = d.get("commands", [])
    if not isinstance(cmds, list):
        cmds = []
    clean = []
    for c in cmds:
        if not isinstance(c, dict):
            continue
        cmd = (c.get("command") or "").strip()
        if not cmd:
            continue
        clean.append({
            "name":    (c.get("name") or cmd[:24]).strip(),
            "command": cmd,
            "sudo":    bool(c.get("sudo", False)),
            "confirm": (c.get("confirm") or "").strip(),
        })
    d["commands"]     = clean
    d["metrics_port"] = int(d.get("metrics_port") or 9100)
    return d
