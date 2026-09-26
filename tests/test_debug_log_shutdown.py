"""Regression tests for debug logging across application shutdown."""

from __future__ import annotations

import asyncio
from pathlib import Path

import debug_log
import main
import persistence
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


def _enable(tmp_path, monkeypatch) -> str:
    monkeypatch.setattr(main, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(debug_log, "_debug_handler", None)
    response = asyncio.run(main.toggle_debug(main.DebugIn(enabled=True)))
    return response["path"]


def test_debug_logging_reopens_after_a_second_app_lifespan(tmp_path, monkeypatch) -> None:
    """A restarted app process can enable and write to a fresh debug log."""
    monkeypatch.setattr(main, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(persistence, "_load_startup_devices", lambda: ([], False, False))
    monkeypatch.setattr(main, "DEVICES_FILE", str(tmp_path / "missing-devices.json"))

    async def _wait_until_cancelled() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "_metrics_loop", _wait_until_cancelled)
    monkeypatch.setattr(main, "_idle_loop", _wait_until_cancelled)

    async def _run_lifespans() -> str:
        async with main.lifespan(main.app):
            first = await main.toggle_debug(main.DebugIn(enabled=True))
            assert first["ok"] is True
            assert debug_log._debug_handler is not None

        assert debug_log._debug_handler is None

        async with main.lifespan(main.app):
            second = await main.toggle_debug(main.DebugIn(enabled=True))
            assert second["ok"] is True
            assert debug_log._debug_handler is not None
            debug_log._debug_write("second lifespan write")
            return second["path"]

    log_path = Path(asyncio.run(_run_lifespans()))

    assert "second lifespan write" in log_path.read_text(encoding="utf-8")


def test_shutdown_clears_handler_when_close_raises(tmp_path, monkeypatch) -> None:
    """A close error cannot retain the handler into a later app lifespan."""
    _enable(tmp_path, monkeypatch)
    handler = debug_log._debug_handler

    def _broken_close() -> None:
        raise OSError("close failed")

    monkeypatch.setattr(handler, "close", _broken_close)

    try:
        debug_log.close_on_shutdown()
    except OSError:
        pass

    assert debug_log._debug_handler is None
