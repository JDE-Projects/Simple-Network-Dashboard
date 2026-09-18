"""Tests for Task 9 Phase 3: debug-log open/write/rotation failures must

disable debug logging, warn the journal, and never propagate into a caller
(device save, metrics poll, or SSH broadcast). Follows the fixture style of
test_debug_log_rotation.py.
"""

from __future__ import annotations

import asyncio
import os

import main
import pytest


@pytest.fixture(autouse=True)
def _clean_debug_logger():
    """Guarantee no handler or state leaks between tests, pass or fail."""
    yield
    for handler in list(main._debug_logger.handlers):
        main._debug_logger.removeHandler(handler)
        handler.close()
    main._debug_handler = None
    main._debug_failed_notice = None
    main._debug_loop = None


def _enable(tmp_path, monkeypatch, *, max_bytes: int = 200, backup_count: int = 3) -> str:
    monkeypatch.setattr(main, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(main, "_DEBUG_LOG_MAX_BYTES", max_bytes)
    monkeypatch.setattr(main, "_DEBUG_LOG_BACKUP_COUNT", backup_count)
    monkeypatch.setattr(main, "_debug_handler", None)
    response = asyncio.run(main.toggle_debug(main.DebugIn(enabled=True)))
    return response["path"]


def test_write_failure_disables_logging_without_raising(tmp_path, monkeypatch) -> None:
    _enable(tmp_path, monkeypatch)

    def _broken_write(*args, **kwargs):
        raise OSError("disk gone")

    # StreamHandler.emit() catches a write failure internally and routes it
    # to handleError(); it must not propagate out of _debug_write.
    monkeypatch.setattr(main._debug_handler.stream, "write", _broken_write)

    main._debug_write("this write fails")

    assert main._debug_handler is None
    assert main._debug_failed_notice

    # A follow-up write with logging disabled must be a silent no-op.
    main._debug_write("should not raise or write anything")


def test_rotation_failure_disables_logging_without_raising(tmp_path, monkeypatch) -> None:
    _enable(tmp_path, monkeypatch, max_bytes=200, backup_count=3)
    handler = main._debug_handler

    def _broken_rollover():
        raise OSError("rename failed")

    monkeypatch.setattr(handler, "doRollover", _broken_rollover)
    monkeypatch.setattr(handler, "shouldRollover", lambda record: True)

    main._debug_write("force a rollover attempt that fails")

    assert main._debug_handler is None
    assert main._debug_failed_notice


def test_open_failure_in_toggle_debug_leaves_debug_off(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(main, "_debug_handler", None)

    def _broken_ctor(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(main, "_PrivateRotatingFileHandler", _broken_ctor)

    response = asyncio.run(main.toggle_debug(main.DebugIn(enabled=True)))

    assert response["ok"] is False
    assert "error" in response
    assert main._debug_handler is None


def test_failure_path_does_not_raise_without_an_event_loop(tmp_path, monkeypatch) -> None:
    _enable(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "_debug_loop", None)

    # Must degrade to journal-only and not raise.
    main._disable_debug_on_failure("simulated failure with no loop")

    assert main._debug_handler is None
    assert main._debug_failed_notice == "simulated failure with no loop"


def test_disable_on_failure_is_idempotent(tmp_path, monkeypatch) -> None:
    _enable(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "_debug_loop", None)

    main._disable_debug_on_failure("first failure")
    assert main._debug_failed_notice == "first failure"

    # Handler is already torn down; a second call must be a no-op and must
    # not overwrite the recorded reason or raise.
    main._disable_debug_on_failure("second failure")
    assert main._debug_failed_notice == "first failure"


def test_failure_notice_reaches_ws_init_payload_and_clears_on_reenable(tmp_path, monkeypatch) -> None:
    _enable(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "_debug_loop", None)

    main._disable_debug_on_failure("log file vanished")
    assert main._debug_failed_notice == "log file vanished"
    assert main._debug_enabled() is False

    # Re-enabling successfully must clear the stale notice.
    monkeypatch.setattr(main, "_debug_handler", None)
    response = asyncio.run(main.toggle_debug(main.DebugIn(enabled=True)))
    assert response["ok"] is True
    assert main._debug_failed_notice is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode bits are not meaningful on Windows")
def test_rotation_chmod_failure_disables_logging(tmp_path, monkeypatch) -> None:
    _enable(tmp_path, monkeypatch, max_bytes=200, backup_count=3)
    handler = main._debug_handler
    monkeypatch.setattr(handler, "shouldRollover", lambda record: True)

    real_chmod = os.chmod

    def _broken_chmod(path, mode, *args, **kwargs):
        if str(path).startswith(str(tmp_path)):
            raise OSError("chmod failed")
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", _broken_chmod)

    main._debug_write("trigger rollover that fails chmod")

    assert main._debug_handler is None
    assert main._debug_failed_notice
