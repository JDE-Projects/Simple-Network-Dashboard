"""Private, server-side browser session storage and login throttling."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Callable


SESSION_STATE_VERSION = 1
SESSION_ONLY_SECONDS = 24 * 60 * 60
REMEMBERED_SECONDS = 30 * 24 * 60 * 60
TOKEN_BYTES = 32
_DIGEST_LENGTH = 64


class SessionStorageError(ValueError):
    """Raised when private session storage is malformed or unsafe."""


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _validate_record(record: object) -> dict[str, object]:
    if not isinstance(record, dict) or set(record) != {"generation", "expires_at"}:
        raise SessionStorageError("session record has an unexpected shape")
    generation = record["generation"]
    expires_at = record["expires_at"]
    if not isinstance(generation, str) or not generation:
        raise SessionStorageError("session record has an invalid generation")
    if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at <= 0:
        raise SessionStorageError("session record has an invalid expiry")
    return record


def validate_session_state(state: object) -> dict[str, object]:
    if not isinstance(state, dict) or set(state) != {"version", "sessions"}:
        raise SessionStorageError("session state has an unexpected shape")
    if state["version"] != SESSION_STATE_VERSION or not isinstance(state["sessions"], dict):
        raise SessionStorageError("session state has an unsupported version or sessions")
    for digest, record in state["sessions"].items():
        if not isinstance(digest, str) or len(digest) != _DIGEST_LENGTH:
            raise SessionStorageError("session state has an invalid token digest")
        try:
            int(digest, 16)
        except ValueError as error:
            raise SessionStorageError("session state has an invalid token digest") from error
        _validate_record(record)
    return state


def _read_private_state(path: Path) -> dict[str, object]:
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
        raise SessionStorageError(f"session state is not a safe regular file: {path}")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise SessionStorageError(f"session state changed while being read: {path}")
        with os.fdopen(fd, "r", encoding="utf-8") as session_file:
            fd = -1
            try:
                return validate_session_state(json.load(session_file))
            except json.JSONDecodeError as error:
                raise SessionStorageError("session state contains invalid JSON") from error
    finally:
        if fd != -1:
            os.close(fd)


class SessionStore:
    """Durably stores only digests for opaque browser session tokens."""

    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.RLock()

    @staticmethod
    def token_digest(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def _load(self) -> dict[str, object]:
        if not os.path.lexists(self.path):
            return {"version": SESSION_STATE_VERSION, "sessions": {}}
        return _read_private_state(self.path)

    def _publish(self, state: dict[str, object]) -> None:
        validate_session_state(state)
        directory = self.path.parent
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise SessionStorageError(f"session directory is unsafe: {directory}")
        staged = directory / f".{self.path.name}.{secrets.token_hex(16)}.tmp"
        fd = -1
        try:
            payload = (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("could not write session state")
                view = view[written:]
            os.fchmod(fd, 0o600)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(staged, self.path)
            _fsync_directory(directory)
        except Exception:
            if fd != -1:
                os.close(fd)
            try:
                os.unlink(staged)
            except OSError:
                pass
            raise

    def create(self, generation: str, remembered: bool) -> tuple[str, int]:
        with self._lock:
            now = int(self.clock())
            expires_at = now + (REMEMBERED_SECONDS if remembered else SESSION_ONLY_SECONDS)
            state = self._load()
            token = secrets.token_urlsafe(TOKEN_BYTES)
            state["sessions"][self.token_digest(token)] = {"generation": generation, "expires_at": expires_at}
            self._publish(state)
            return token, expires_at

    def validate(self, token: str | None, generation: str) -> bool:
        if not token:
            return False
        with self._lock:
            state = self._load()
            sessions = state["sessions"]
            digest = self.token_digest(token)
            record = sessions.get(digest)
            now = int(self.clock())
            changed = False
            for key, value in list(sessions.items()):
                if value["expires_at"] <= now or value["generation"] != generation:
                    del sessions[key]
                    changed = True
            valid = record is not None and digest in sessions
            if changed:
                self._publish(state)
            return valid

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            if not os.path.lexists(self.path):
                return
            state = self._load()
            if state["sessions"].pop(self.token_digest(token), None) is not None:
                self._publish(state)


class LoginThrottle:
    """In-memory, expiring progressive delay keyed by trusted client address."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._failures: dict[str, tuple[int, float]] = {}

    def _current(self, address: str) -> tuple[int, float] | None:
        item = self._failures.get(address)
        if item and self.clock() - item[1] >= 15 * 60:
            del self._failures[address]
            return None
        return item

    def retry_after(self, address: str) -> int:
        item = self._current(address)
        if item is None or item[0] < 5:
            return 0
        delay = min(5 * (2 ** (item[0] - 5)), 300)
        remaining = delay - (self.clock() - item[1])
        return max(0, math.ceil(remaining))

    def failure(self, address: str) -> int:
        item = self._current(address)
        failures = 1 if item is None else item[0] + 1
        self._failures[address] = (failures, self.clock())
        return self.retry_after(address)

    def success(self, address: str) -> None:
        self._failures.pop(address, None)
