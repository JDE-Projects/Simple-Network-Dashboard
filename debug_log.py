import asyncio
import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Optional


# Debug log rotation: one active file plus this many rotated copies, each
# capped at this size (roughly 20 MB retained in total at the defaults).
_DEBUG_LOG_MAX_BYTES = 5 * 1024 * 1024
_DEBUG_LOG_BACKUP_COUNT = 3


def _debug_log_opener(path: str, _flags: int) -> int:
    """Force-create every debug log file (active or freshly rotated) as a
    private mode-0600 file, regardless of the flags logging would otherwise
    use."""
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that guarantees mode 0600 on the active file and
    on every rotated copy. Renames preserve permissions on their own, but we
    re-apply 0600 explicitly after each rollover so that guarantee can't be
    silently lost by a future change."""

    def _open(self):
        return open(self.baseFilename, self.mode, encoding=self.encoding, opener=_debug_log_opener)

    def doRollover(self):
        super().doRollover()
        for i in range(1, self.backupCount + 1):
            rotated = f"{self.baseFilename}.{i}"
            if os.path.exists(rotated):
                os.chmod(rotated, 0o600)
        if os.path.exists(self.baseFilename):
            os.chmod(self.baseFilename, 0o600)

    def handleError(self, record):
        # The logging framework routes both write failures and doRollover
        # failures here instead of letting them propagate. Default behavior
        # is a silent stderr traceback; turn it into a hard, visible disable
        # instead so a broken log file never keeps silently failing.
        _disable_debug_on_failure("debug log write or rotation failed")


# Debug log handler, None when disabled
_debug_handler: Optional[RotatingFileHandler] = None
_debug_logger = logging.getLogger("simple_network_dashboard.debug")
_debug_logger.setLevel(logging.DEBUG)
_debug_logger.propagate = False

# Event loop the debug-failure path can use to reach browser tabs from an
# SSH worker thread. Set in lifespan(), alongside ssh_mgr.set_loop().
_debug_loop: Optional[asyncio.AbstractEventLoop] = None

# Reason the debug log was last disabled by a failure, shown to any tab that
# connects afterward. Cleared once debug logging is successfully re-enabled.
_debug_failed_notice: Optional[str] = None
_broadcaster = None


def configure(broadcaster, loop):
    global _broadcaster, _debug_loop
    _broadcaster = broadcaster
    _debug_loop = loop


def _debug_enabled() -> bool:
    return _debug_handler is not None


def _debug_write(text: str):
    if _debug_handler is not None:
        _debug_logger.debug(text)


def _disable_debug_on_failure(reason: str):
    """Shared failure path for debug-log open/write/rotation errors.

    Safe to call from any thread (including SSH worker threads) and must
    never raise. Tears down the broken handler, warns the journal, and
    pushes a visible warning to every connected browser tab. Idempotent:
    once _debug_handler is None, later calls are a no-op so several writes
    failing in a row don't spam the journal or the tabs repeatedly."""
    global _debug_handler, _debug_failed_notice

    if _debug_handler is None:
        return

    handler = _debug_handler
    _debug_handler = None
    _debug_failed_notice = reason

    try:
        _debug_logger.removeHandler(handler)
    except Exception:
        pass
    try:
        handler.close()
    except Exception:
        pass

    try:
        print(f"WARNING: debug logging disabled ({reason}). See the server log directory.", file=sys.stderr, flush=True)
    except Exception:
        pass

    try:
        loop = _debug_loop
        if loop is not None and not loop.is_closed() and _broadcaster is not None:
            asyncio.run_coroutine_threadsafe(
                _broadcaster({"type": "debug_failed", "reason": reason}), loop
            )
    except Exception:
        pass


def enable(log_dir) -> dict:
    global _debug_handler, _debug_failed_notice
    path = os.path.join(log_dir, f"Debug_Log_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.txt")
    handler = None
    try:
        handler = _PrivateRotatingFileHandler(
            path,
            mode="w",
            maxBytes=_DEBUG_LOG_MAX_BYTES,
            backupCount=_DEBUG_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(fmt="[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
        _debug_logger.addHandler(handler)
    except Exception as e:
        if handler is not None:
            try:
                _debug_logger.removeHandler(handler)
            except Exception:
                pass
        print(f"WARNING: could not enable debug logging ({e}). See the server log directory.", file=sys.stderr, flush=True)
        return {"ok": False, "error": f"Could not open the debug log file ({type(e).__name__})."}
    _debug_handler = handler
    _debug_failed_notice = None
    _debug_write("=== Debug log started ===")
    return {"ok": True, "enabled": True, "path": path}


def disable() -> None:
    global _debug_handler
    _debug_write("=== Debug log stopped ===")
    _debug_logger.removeHandler(_debug_handler)
    _debug_handler.close()
    _debug_handler = None


def close_on_shutdown() -> None:
    if _debug_handler is not None:
        _debug_write("=== Debug log closed (server shutdown) ===")
        _debug_logger.removeHandler(_debug_handler)
        _debug_handler.close()


def get_failed_notice():
    return _debug_failed_notice
