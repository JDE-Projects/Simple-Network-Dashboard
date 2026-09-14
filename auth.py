"""Password-state creation and validation for Simple Network Dashboard."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import unicodedata
from pathlib import Path

from argon2 import PasswordHasher, Type, extract_parameters
from argon2.exceptions import HashingError, InvalidHashError


AUTH_STATE_VERSION = 1
MIN_PASSWORD_LENGTH = 15
MAX_PASSWORD_LENGTH = 128
MEMORY_COST_KIB = 19_456
TIME_COST = 2
PARALLELISM = 1
_AUTH_HASH_RE = re.compile(
    r"^\$argon2id\$v=19\$m=19456,t=2,p=1\$[A-Za-z0-9+/]{22}\$[A-Za-z0-9+/]{43}$"
)
_SESSION_GENERATION_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class PasswordPolicyError(ValueError):
    """Raised when a password is unsuitable for dashboard authentication."""


class AuthStatePublicationError(OSError):
    """Reports whether a failed publication had already replaced the state."""

    def __init__(self, published: bool, cause: BaseException):
        self.published = published
        message = (
            "authentication state was published but directory durability could not be confirmed"
            if published
            else "authentication state was not published; the prior state was retained"
        )
        super().__init__(message)
        self.__cause__ = cause


PASSWORD_HASHER = PasswordHasher(
    time_cost=TIME_COST,
    memory_cost=MEMORY_COST_KIB,
    parallelism=PARALLELISM,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


def normalize_password(password: str) -> str:
    """Normalize and validate a password without imposing composition rules."""
    normalized = unicodedata.normalize("NFC", password)
    if not MIN_PASSWORD_LENGTH <= len(normalized) <= MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be {MIN_PASSWORD_LENGTH} through {MAX_PASSWORD_LENGTH} characters."
        )
    return normalized


def prompt_for_password() -> str:
    """Read a new password twice without echoing either entry."""
    try:
        password = normalize_password(getpass.getpass("Dashboard password: "))
        confirmation = getpass.getpass("Confirm dashboard password: ")
    except (KeyboardInterrupt, EOFError) as error:
        raise PasswordPolicyError("Password entry was interrupted; no authentication state was published.") from error
    if password != unicodedata.normalize("NFC", confirmation):
        raise PasswordPolicyError("Passwords did not match.")
    return password


def create_auth_state(password: str) -> dict[str, object]:
    """Create a complete replacement authentication state from a password."""
    normalized = normalize_password(password)
    return {
        "version": AUTH_STATE_VERSION,
        "password_hash": PASSWORD_HASHER.hash(normalized),
        "session_generation": secrets.token_urlsafe(32),
    }


def validate_auth_state(state: object) -> dict[str, object]:
    """Reject malformed or incompatible authentication state before preserving it."""
    if not isinstance(state, dict) or set(state) != {
        "version",
        "password_hash",
        "session_generation",
    }:
        raise ValueError("authentication state has an unexpected shape")
    if state["version"] != AUTH_STATE_VERSION:
        raise ValueError("authentication state has an unsupported version")
    password_hash = state["password_hash"]
    session_generation = state["session_generation"]
    if not isinstance(password_hash, str) or not _AUTH_HASH_RE.fullmatch(password_hash):
        raise ValueError("authentication state has an invalid Argon2id hash")
    try:
        parameters = extract_parameters(password_hash)
    except InvalidHashError as error:
        raise ValueError("authentication state has an invalid Argon2id hash") from error
    if (
        parameters.type is not Type.ID
        or parameters.memory_cost != MEMORY_COST_KIB
        or parameters.time_cost != TIME_COST
        or parameters.parallelism != PARALLELISM
        or parameters.salt_len != 16
        or parameters.hash_len != 32
    ):
        raise ValueError("authentication state has unsupported Argon2id parameters")
    if not isinstance(session_generation, str) or not _SESSION_GENERATION_RE.fullmatch(session_generation):
        raise ValueError("authentication state has an invalid session generation")
    return state


def _read_private_state(path: Path) -> dict[str, object]:
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"authentication state is not a safe regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ValueError(f"authentication state changed while being read: {path}")
        with os.fdopen(fd, "r", encoding="utf-8") as state_file:
            fd = -1
            return validate_auth_state(json.load(state_file))
    finally:
        if fd != -1:
            os.close(fd)


def load_auth_state(path: str | Path) -> dict[str, object]:
    return _read_private_state(Path(path))


def _fsync_directory(directory: Path) -> None:
    # Windows does not permit a directory handle to be opened this way. The
    # deployed dashboard runs on Linux, where this is required for durability.
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_auth_state(
    path: str | Path, state: dict[str, object], owner: tuple[int, int] | None = None
) -> None:
    """Durably and atomically replace a validated state without a password backup."""
    destination = Path(path)
    directory = destination.parent
    staged: Path | None = None
    fd = -1
    published = False
    try:
        directory_info = os.lstat(directory)
        if not stat.S_ISDIR(directory_info.st_mode) or stat.S_ISLNK(directory_info.st_mode):
            raise ValueError(f"authentication directory is unsafe: {directory}")
        validate_auth_state(state)
        payload = (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        staged = directory / f".{destination.name}.{secrets.token_hex(16)}.tmp"
        fd = os.open(
            staged,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("could not write authentication state")
            view = view[written:]
        os.fchmod(fd, 0o600)
        if owner is not None:
            os.fchown(fd, *owner)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(staged, destination)
        published = True
        _fsync_directory(directory)
    except Exception as error:
        if fd != -1:
            os.close(fd)
        if not published and staged is not None:
            try:
                os.unlink(staged)
            except OSError:
                pass
        raise AuthStatePublicationError(published, error) from error


def _require_root() -> None:
    if os.geteuid() != 0:
        raise PermissionError("snd-reset-password must be run as root with sudo.")


def _service_owner_ids() -> tuple[int, int] | None:
    if os.name == "nt":
        return None
    import grp
    import pwd

    try:
        return (pwd.getpwnam("snd").pw_uid, grp.getgrnam("snd").gr_gid)
    except KeyError as error:
        raise RuntimeError("The snd service account is unavailable; authentication state was not changed.") from error


def _initialize(path: Path) -> None:
    if path.exists() or path.is_symlink():
        load_auth_state(path)
        print("Existing dashboard password state was preserved.")
        return
    publish_auth_state(path, create_auth_state(prompt_for_password()), _service_owner_ids())
    print("Dashboard password initialized.")


def _reset(path: Path, service_name: str) -> None:
    _require_root()
    load_auth_state(path)
    replacement = create_auth_state(prompt_for_password())
    owner = _service_owner_ids()
    result = subprocess.run(["systemctl", "stop", service_name], check=False)
    if result.returncode != 0:
        raise RuntimeError("Dashboard service could not be stopped; password was not changed.")
    try:
        publish_auth_state(path, replacement, owner)
    except AuthStatePublicationError as error:
        if error.published:
            raise RuntimeError(
                "Password was changed, but directory durability could not be confirmed. "
                f"The previous process was stopped; run: sudo systemctl status {service_name}"
            ) from error
        raise RuntimeError(
            "Password was not changed and the dashboard service was kept stopped. "
            f"Run: sudo systemctl start {service_name}"
        ) from error
    result = subprocess.run(["systemctl", "start", service_name], check=False)
    active = subprocess.run(["systemctl", "is-active", "--quiet", service_name], check=False)
    if result.returncode != 0 or active.returncode != 0:
        raise RuntimeError(
            "Password was changed but dashboard startup could not be confirmed. "
            f"Run: sudo systemctl status {service_name}, then sudo systemctl restart {service_name}"
        )
    print("Dashboard password reset and service started.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage Simple Network Dashboard password state.")
    parser.add_argument("command", choices=("initialize", "validate", "reset"))
    parser.add_argument("--auth-file", required=True)
    parser.add_argument("--service-name", default="simple-network-dashboard")
    args = parser.parse_args()
    path = Path(args.auth_file)
    try:
        if args.command == "initialize":
            _initialize(path)
        elif args.command == "validate":
            load_auth_state(path)
        else:
            _reset(path, args.service_name)
    except (KeyboardInterrupt, EOFError):
        print(
            "Error: authentication management was interrupted; inspect the authentication file and service status.",
            file=sys.stderr,
        )
        return 1
    except (
        OSError,
        ValueError,
        PasswordPolicyError,
        PermissionError,
        RuntimeError,
        HashingError,
        json.JSONDecodeError,
    ) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
