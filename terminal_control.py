"""Terminal control for a running command: input, resize, output
acknowledgements, cancel, and page-refresh recovery. Mixed into SSHManager,
whose sessions and per-device locks these methods use."""

import threading
import time

from terminal_history import trim_to_budget
from terminal_run import clamp_size
import config


class TerminalControlMixin:
    def claim_terminal(self, device_id: str, run_id: str, owner: str, socket_id: str) -> dict:
        with self._device_lock(device_id):
            sess = self.sessions.get(device_id)
            if not sess or sess.owner != owner or sess.state != "connected" or not sess.busy or sess.run_id != run_id:
                self._debug_write(f"SSH [{self._device_tag(device_id, sess)}] terminal input refused")
                return {"ok": False}
            sess.controller = socket_id
            sess.control_rev += 1
            if sess.writer:
                sess.writer.clear_input()
            return {"ok": True, "rev": sess.control_rev}

    def terminal_input(self, device_id, run_id, rev, data, owner, socket_id) -> dict:
        with self._device_lock(device_id):
            sess = self.sessions.get(device_id)
            valid = (isinstance(data, str) and len(data) <= config.TERM_INPUT_MAX_CHARS and sess
                     and sess.owner == owner and sess.state == "connected" and sess.busy
                     and sess.run_id == run_id and sess.controller == socket_id
                     and sess.control_rev == rev and sess.auth_complete and sess.writer is not None)
            if not valid:
                self._debug_write(f"SSH [{self._device_tag(device_id, sess)}] terminal input refused")
                return {"ok": False}
            if not sess.writer.input(data):
                return {"ok": False, "full": True}
            sess.last_io = time.monotonic()
            return {"ok": True}

    def terminal_resize(self, device_id, run_id, rev, cols, rows, owner, socket_id) -> dict:
        with self._device_lock(device_id):
            sess = self.sessions.get(device_id)
            valid = (isinstance(cols, int) and not isinstance(cols, bool) and isinstance(rows, int) and not isinstance(rows, bool)
                     and sess and sess.owner == owner and sess.state == "connected" and sess.busy
                     and sess.run_id == run_id and sess.controller == socket_id and sess.control_rev == rev
                     and sess.auth_complete and sess.writer is not None)
            if not valid:
                self._debug_write(f"SSH [{self._device_tag(device_id, sess)}] terminal input refused")
                return {"ok": False}
            sess.cols, sess.rows = clamp_size(cols, rows)
            sess.writer.resize(sess.cols, sess.rows)
            return {"ok": True}

    def terminal_ack(self, device_id, run_id, seq, owner) -> None:
        """Record completed browser terminal writes for the current owner run."""
        if not isinstance(seq, int) or isinstance(seq, bool):
            return
        with self._device_lock(device_id):
            sess = self.sessions.get(device_id)
            if (sess is None or sess.owner != owner or sess.state != "connected"
                    or not sess.busy or sess.run_id != run_id):
                return
            with sess.term_condition:
                if seq > sess.sent_seq or seq <= sess.acked_seq:
                    return
                sess.acked_seq = seq
                sess.ack_progress_at = time.monotonic()
                sess.ack_stalled = False
                for sent_seq in [item for item in sess.sent_bytes if item <= seq]:
                    del sess.sent_bytes[sent_seq]
                sess.term_condition.notify_all()

    def _wait_for_terminal_ack(self, device_id, sess, run_id) -> bool:
        """Pause SSH reading while the browser has too much unwritten output."""
        with sess.term_condition:
            while True:
                if self.sessions.get(device_id) is not sess or sess.run_id != run_id:
                    return False
                if sess.cancelled:
                    return True
                if not self._owner_watching(sess.owner) or sess.ack_stalled:
                    return True
                if sum(sess.sent_bytes.values()) <= config.TERM_UNACKED_MAX_BYTES:
                    return True
                remaining = config.TERM_ACK_STALL_SECONDS - (time.monotonic() - sess.ack_progress_at)
                if remaining <= 0:
                    sess.ack_stalled = True
                    self._debug_write("terminal acknowledgements stalled")
                    return True
                sess.term_condition.wait(min(0.25, remaining))

    def cancel(self, device_id: str, run_id: str = None, owner: str = None) -> dict:
        with self._device_lock(device_id):
            sess = self.sessions.get(device_id)
            if not sess or not sess.busy or sess.run_id != run_id:
                return {"ok": False}
            if owner is not None and sess.owner != owner:
                return {"ok": False, "not_owner": True,
                        "error": "This device is in use by another session."}
            # `cancel_requested` is honored by _exec even before sess.channel
            # is wired up, so a cancel arriving in that early window still
            # takes effect instead of being silently dropped.
            sess.cancelled = True
            sess.cancel_requested = True
            with sess.term_condition:
                sess.term_condition.notify_all()
            channel = sess.channel
            ready = channel is not None and sess.auth_complete and sess.writer is not None
            if ready and sess.stop_timer is not None:
                sess.forced = True
                channel.close()
                return {"ok": True}
            if ready:
                sess.writer.interrupt()
                timer = threading.Timer(config.TERM_STOP_GRACE_SECONDS, self._force_stop,
                                        args=(device_id, sess, run_id, channel))
                timer.daemon = True
                sess.stop_timer = timer
                timer.start()
                return {"ok": True}
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass
        return {"ok": True}

    def _force_stop(self, device_id, sess, run_id, channel):
        with self._device_lock(device_id):
            if self.sessions.get(device_id) is not sess or sess.run_id != run_id or sess.channel is not channel:
                return
            sess.forced = True
            channel.close()

    def owner_terminal_snapshot(self, owner: str) -> list[dict]:
        """Return recoverable terminal state for this owner's current runs."""
        terminals = []
        for device_id in list(self.sessions):
            with self._device_lock(device_id):
                sess = self.sessions.get(device_id)
                if (sess is None or sess.owner != owner or sess.state != "connected"
                        or not sess.busy or sess.history is None):
                    continue
                terminals.append({
                    "device_id": device_id,
                    "run_id": sess.run_id,
                    "cols": sess.cols,
                    "rows": sess.rows,
                    "label": sess.label,
                    **sess.history.snapshot(),
                })
                with sess.term_condition:
                    if sess.sent_seq > sess.acked_seq:
                        sess.acked_seq = sess.sent_seq
                        sess.ack_progress_at = time.monotonic()
                        sess.ack_stalled = False
                        sess.sent_bytes.clear()
                        sess.term_condition.notify_all()
        return trim_to_budget(terminals, config.TERM_INIT_HISTORY_MAX_CHARS)

    def request_terminal_redraw(self, device_id: str, run_id: str, owner: str) -> bool:
        """Request a full-screen program redraw through its writer thread."""
        with self._device_lock(device_id):
            sess = self.sessions.get(device_id)
            if (sess is None or sess.owner != owner or sess.run_id != run_id
                    or sess.writer is None or sess.rows is None):
                return False
            sess.writer.resize(sess.cols, sess.rows - 1)
            sess.writer.resize(sess.cols, sess.rows)
            return True
