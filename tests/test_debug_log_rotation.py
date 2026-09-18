"""Tests for the size-based debug log rotation added in main.py.

Exercises _PrivateRotatingFileHandler and the /api/debug toggle (called
directly as a coroutine, the same pattern test_private_storage.py uses)
against a private LOG_DIR under tmp_path with a tiny maxBytes so rollovers
happen after a handful of lines instead of 5 MB of writes.
"""

from __future__ import annotations

import asyncio
import os
import stat

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


def _enable(tmp_path, monkeypatch, *, max_bytes: int = 200, backup_count: int = 3) -> str:
    monkeypatch.setattr(main, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(main, "_DEBUG_LOG_MAX_BYTES", max_bytes)
    monkeypatch.setattr(main, "_DEBUG_LOG_BACKUP_COUNT", backup_count)
    monkeypatch.setattr(main, "_debug_handler", None)
    response = asyncio.run(main.toggle_debug(main.DebugIn(enabled=True)))
    return response["path"]


def _disable() -> None:
    asyncio.run(main.toggle_debug(main.DebugIn(enabled=False)))


def test_debug_write_is_a_noop_when_logging_is_off(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(main, "_debug_handler", None)
    assert not main._debug_enabled()

    main._debug_write("should not raise or write anything")

    assert list(tmp_path.iterdir()) == []


def test_start_and_stop_markers_are_written(tmp_path, monkeypatch) -> None:
    path = _enable(tmp_path, monkeypatch)
    _disable()

    content = open(path, encoding="utf-8").read()
    assert "=== Debug log started ===" in content
    assert "=== Debug log stopped ===" in content


def test_writing_past_the_size_threshold_triggers_a_rollover(tmp_path, monkeypatch) -> None:
    path = _enable(tmp_path, monkeypatch, max_bytes=200, backup_count=3)

    for i in range(30):
        main._debug_write(f"line {i} padded out with filler text to add bulk")

    _disable()

    assert os.path.exists(f"{path}.1")


def test_no_more_than_three_rotated_copies_are_retained(tmp_path, monkeypatch) -> None:
    path = _enable(tmp_path, monkeypatch, max_bytes=200, backup_count=3)

    for i in range(200):
        main._debug_write(f"line {i} padded out with filler text to add bulk")

    _disable()

    assert os.path.exists(f"{path}.1")
    assert os.path.exists(f"{path}.2")
    assert os.path.exists(f"{path}.3")
    assert not os.path.exists(f"{path}.4")


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode bits are not meaningful on Windows")
def test_active_and_rotated_files_are_private(tmp_path, monkeypatch) -> None:
    path = _enable(tmp_path, monkeypatch, max_bytes=200, backup_count=3)

    for i in range(60):
        main._debug_write(f"line {i} padded out with filler text to add bulk")

    _disable()

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    for i in (1, 2, 3):
        rotated = f"{path}.{i}"
        assert os.path.exists(rotated)
        assert stat.S_IMODE(os.stat(rotated).st_mode) == 0o600
