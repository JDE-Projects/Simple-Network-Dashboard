"""Tests for Task 9 Phase 3: debug-log open/write/rotation failures must

disable debug logging, warn the journal, and never propagate into a caller
(device save, metrics poll, or SSH broadcast). Follows the fixture style of
test_debug_log_rotation.py.
"""

from __future__ import annotations

import asyncio
import os

import debug_log
import main
import pytest


@pytest.fixture(autouse=True)
def _clean_debug_logger():
    """Guarantee no handler or state leaks between tests, pass or fail."""
    yield
    for handler in list(debug_log._debug_logger.handlers):
        debug_log._debug_logger.removeHandler(handler)
        handler.close()
    debug_log._debug_handler = None
    debug_log._debug_failed_notice = None
    debug_log._debug_loop = None


def _enable(tmp_path, monkeypatch, *, max_bytes: int = 200, backup_count: int = 3) -> str:
    monkeypatch.setattr(main, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(debug_log, "_DEBUG_LOG_MAX_BYTES", max_bytes)
    monkeypatch.setattr(debug_log, "_DEBUG_LOG_BACKUP_COUNT", backup_count)
    monkeypatch.setattr(debug_log, "_debug_handler", None)
    response = asyncio.run(main.toggle_debug(main.DebugIn(enabled=True)))
    return response["path"]


def test_write_failure_disables_logging_without_raising(tmp_path, monkeypatch) -> None:
    _enable(tmp_path, monkeypatch)

    def _broken_write(*args, **kwargs):
        raise OSError("disk gone")

    # StreamHandler.emit() catches a write failure internally and routes it
    # to handleError(); it must not propagate out of _debug_write.
    monkeypatch.setattr(debug_log._debug_handler.stream, "write", _broken_write)

    debug_log._debug_write("this write fails")

    assert debug_log._debug_handler is None
    assert debug_log._debug_failed_notice

    # A follow-up write with logging disabled must be a silent no-op.
    debug_log._debug_write("should not raise or write anything")


def test_rotation_failure_disables_logging_without_raising(tmp_path, monkeypatch) -> None:
    _enable(tmp_path, monkeypatch, max_bytes=200, backup_count=3)
    handler = debug_log._debug_handler

    def _broken_rollover():
        raise OSError("rename failed")

    monkeypatch.setattr(handler, "doRollover", _broken_rollover)
    monkeypatch.setattr(handler, "shouldRollover", lambda record: True)

    debug_log._debug_write("force a rollover attempt that fails")

    assert debug_log._debug_handler is None
    assert debug_log._debug_failed_notice


def test_open_failure_in_toggle_debug_leaves_debug_off(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(debug_log, "_debug_handler", None)

    def _broken_ctor(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(debug_log, "_PrivateRotatingFileHandler", _broken_ctor)

    response = asyncio.run(main.toggle_debug(main.DebugIn(enabled=True)))

    assert response["ok"] is False
    assert "error" in response
    assert debug_log._debug_handler is None


def test_failure_path_does_not_raise_without_an_event_loop(tmp_path, monkeypatch) -> None:
    _enable(tmp_path, monkeypatch)
    monkeypatch.setattr(debug_log, "_debug_loop", None)

    # Must degrade to journal-only and not raise.
    debug_log._disable_debug_on_failure("simulated failure with no loop")

    assert debug_log._debug_handler is None
    assert debug_log._debug_failed_notice == "simulated failure with no loop"


def test_disable_on_failure_is_idempotent(tmp_path, monkeypatch) -> None:
    _enable(tmp_path, monkeypatch)
    monkeypatch.setattr(debug_log, "_debug_loop", None)

    debug_log._disable_debug_on_failure("first failure")
    assert debug_log._debug_failed_notice == "first failure"

    # Handler is already torn down; a second call must be a no-op and must
    # not overwrite the recorded reason or raise.
    debug_log._disable_debug_on_failure("second failure")
    assert debug_log._debug_failed_notice == "first failure"


def test_failure_notice_reaches_ws_init_payload_and_clears_on_reenable(tmp_path, monkeypatch) -> None:
    _enable(tmp_path, monkeypatch)
    monkeypatch.setattr(debug_log, "_debug_loop", None)

    debug_log._disable_debug_on_failure("log file vanished")
    assert debug_log._debug_failed_notice == "log file vanished"
    assert debug_log._debug_enabled() is False

    # Re-enabling successfully must clear the stale notice.
    monkeypatch.setattr(debug_log, "_debug_handler", None)
    response = asyncio.run(main.toggle_debug(main.DebugIn(enabled=True)))
    assert response["ok"] is True
    assert debug_log._debug_failed_notice is None


def test_broadcast_still_delivers_when_the_debug_write_fails(tmp_path, monkeypatch) -> None:
    """A dashboard action must survive a debug-log failure underneath it.

    _broadcast writes SSH events to the debug log before delivering them to
    tabs. If that write fails, logging is disabled but the broadcast must
    still reach ws_mgr, not abort halfway.
    """
    _enable(tmp_path, monkeypatch)
    monkeypatch.setattr(debug_log, "_debug_loop", None)
    monkeypatch.setattr(debug_log._debug_handler.stream, "write", lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone")))

    delivered = []

    async def _record(msg):
        delivered.append(msg)

    monkeypatch.setattr(main.ws_mgr, "broadcast", _record)

    asyncio.run(main._broadcast({"type": "ssh_log", "device_id": "dev_a", "level": "out", "text": "hi"}))

    # The write failed and disabled logging, but the SSH message still shipped.
    assert debug_log._debug_handler is None
    assert debug_log._debug_failed_notice
    assert delivered == [{"type": "ssh_log", "device_id": "dev_a", "level": "out", "text": "hi"}]


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode bits are not meaningful on Windows")
def test_rotation_chmod_failure_disables_logging(tmp_path, monkeypatch) -> None:
    _enable(tmp_path, monkeypatch, max_bytes=200, backup_count=3)
    handler = debug_log._debug_handler
    monkeypatch.setattr(handler, "shouldRollover", lambda record: True)

    real_chmod = os.chmod

    def _broken_chmod(path, mode, *args, **kwargs):
        if str(path).startswith(str(tmp_path)):
            raise OSError("chmod failed")
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", _broken_chmod)

    debug_log._debug_write("trigger rollover that fails chmod")

    assert debug_log._debug_handler is None
    assert debug_log._debug_failed_notice
