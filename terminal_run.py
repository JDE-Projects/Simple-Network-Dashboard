"""Pure terminal command helpers and channel reader."""

from __future__ import annotations

import codecs
import collections
from dataclasses import dataclass
import secrets
import shlex
import socket
import threading
import time
from threading import Thread as _Thread

import config


def clamp_size(cols, rows) -> tuple[int, int]:
    """Return terminal dimensions within the supported bounds."""
    cols = config.TERM_DEFAULT_COLS if cols is None else cols
    rows = config.TERM_DEFAULT_ROWS if rows is None else rows
    return (
        max(config.TERM_MIN_COLS, min(config.TERM_MAX_COLS, int(cols))),
        max(config.TERM_MIN_ROWS, min(config.TERM_MAX_ROWS, int(rows))),
    )


def build_command(raw_cmd: str, use_sudo: bool) -> tuple[str, str | None, str | None]:
    """Build the optional all-or-nothing sudo wrapper for a command."""
    if not use_sudo:
        return raw_cmd, None, None
    phrase = "SNDsudoprompt" + secrets.token_hex(16)
    mark = "SNDtermmark" + secrets.token_hex(16)
    whole = raw_cmd if raw_cmd.startswith("sudo ") else "sudo bash -c " + shlex.quote(raw_cmd)
    command = (
        "set +x 2>/dev/null; sudo -S -p " + shlex.quote(phrase)
        + " -v || exit 97; printf '%s' " + shlex.quote(mark)
        + "; exec bash -c " + shlex.quote(whole)
    )
    return command, phrase, mark


@dataclass(frozen=True)
class GateEvent:
    send_password: bool = False
    refused: bool = False
    output: bytes = b""


class SudoGate:
    """Discard sudo authentication output until the command-start marker."""

    def __init__(self, phrase: str, mark: str):
        self._phrase = phrase.encode("ascii")
        self._mark = mark.encode("ascii")
        self._pending = b""
        self._asked = False
        self._refused = False
        self.passed = False

    def _suffix_prefix(self, data: bytes) -> bytes:
        keep = min(len(data), max(len(self._phrase), len(self._mark)) - 1)
        for size in range(keep, 0, -1):
            suffix = data[-size:]
            if self._phrase.startswith(suffix) or self._mark.startswith(suffix):
                return suffix
        return b""

    def feed(self, data: bytes) -> GateEvent:
        if self.passed:
            return GateEvent(output=data)
        if self._refused:
            return GateEvent(refused=True)
        buf = self._pending + data
        self._pending = b""
        send_password = False
        while buf:
            phrase_at = buf.find(self._phrase)
            mark_at = buf.find(self._mark)
            candidates = [(at, kind) for at, kind in ((phrase_at, "phrase"), (mark_at, "mark")) if at >= 0]
            if not candidates:
                self._pending = self._suffix_prefix(buf)
                break
            at, kind = min(candidates)
            if kind == "mark":
                self.passed = True
                return GateEvent(send_password=send_password, output=buf[at + len(self._mark):])
            buf = buf[at + len(self._phrase):]
            if self._asked:
                self._refused = True
                return GateEvent(send_password=send_password, refused=True)
            self._asked = True
            send_password = True
        return GateEvent(send_password=send_password)


class OutputBatcher:
    """Incrementally decode and batch terminal output for one run."""

    def __init__(self, clock=time.monotonic):
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._clock = clock
        self._text = ""
        self._started = None
        self._seq = 0

    @property
    def seq(self) -> int:
        return self._seq

    def add(self, data: bytes) -> list[tuple[str, int]]:
        text = self._decoder.decode(data, False)
        if text:
            if not self._text:
                self._started = self._clock()
            self._text += text
        return self.flush() if self._due() else []

    def _due(self) -> bool:
        return bool(self._text) and (
            len(self._text) >= config.TERM_BATCH_MAX_CHARS
            or self._clock() - self._started >= config.TERM_BATCH_INTERVAL
        )

    def flush(self, final: bool = False) -> list[tuple[str, int]]:
        if final:
            tail = self._decoder.decode(b"", True)
            if tail:
                if not self._text:
                    self._started = self._clock()
                self._text += tail
        if not self._text or (not final and not self._due()):
            return []
        self._seq += 1
        result = [(self._text, self._seq)]
        self._text = ""
        self._started = None
        return result


@dataclass(frozen=True)
class ReadResult:
    gate_passed: bool
    refused: bool
    superseded: bool
    exit_code: int


class TerminalWriter:
    """Serialize terminal input, control bytes, and resize operations."""

    def __init__(self, chan, on_stuck):
        self._chan = chan
        self._on_stuck = on_stuck
        self._items = collections.deque()
        self._pending_input = 0
        self._closed = False
        self._condition = threading.Condition()
        self.thread = _Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def input(self, data: str) -> bool:
        encoded = data.encode("utf-8")
        with self._condition:
            if self._closed or self._pending_input + len(encoded) > config.TERM_PENDING_INPUT_MAX_BYTES:
                return False
            self._items.append(("write", encoded, True))
            self._pending_input += len(encoded)
            self._condition.notify()
            return True

    def resize(self, cols: int, rows: int):
        self._put(("resize", cols, rows))

    def password(self, password: str):
        if password:
            done = threading.Event()
            self._put(("write", (password + "\n").encode("utf-8"), False, done))
            return done
        return None

    def interrupt(self):
        with self._condition:
            if not self._closed:
                self._items.appendleft(("write", b"\x03", False))
                self._condition.notify()

    def clear_input(self):
        with self._condition:
            self._items = collections.deque(
                item for item in self._items if item[0] != "write" or not item[2])
            self._pending_input = 0

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def _put(self, item):
        with self._condition:
            if not self._closed:
                self._items.append(item)
                self._condition.notify()

    def _run(self):
        try:
            self._chan.settimeout(config.TERM_SEND_TIMEOUT)
        except Exception:
            pass
        while True:
            with self._condition:
                while not self._items and not self._closed:
                    self._condition.wait(0.1)
                if self._closed:
                    return
                item = self._items.popleft()
                if item[0] == "write" and item[2]:
                    self._pending_input -= len(item[1])
            if item[0] == "resize":
                try:
                    self._chan.resize_pty(width=item[1], height=item[2])
                except Exception:
                    pass
                continue
            data = item[1]
            deadline = time.monotonic() + config.TERM_WRITE_DEADLINE
            stuck = False
            while data and not self._closed:
                if time.monotonic() >= deadline:
                    stuck = True
                    break
                try:
                    sent = self._chan.send(data)
                except socket.timeout:
                    continue
                except Exception:
                    break
                if not sent:
                    # Paramiko returns 0 once the channel is closed.
                    break
                data = data[sent:]
            if len(item) > 3:
                item[3].set()
            if stuck:
                self._on_stuck()
            if data:
                return


def read_channel(chan, gate, batcher, emit, should_continue, send_password,
                 on_gate_passed=None) -> ReadResult:
    """Drain a Paramiko-like channel without retaining terminal output.
    `on_gate_passed` runs once, as soon as the sudo gate sees its marker and
    before any output that follows it, so a silent command still starts."""
    refused = False
    superseded = False
    announced = False

    def handle(raw: bytes) -> bool:
        nonlocal refused, announced
        if gate is not None:
            event = gate.feed(raw)
            if event.send_password:
                send_password()
            if gate.passed and not announced:
                announced = True
                if on_gate_passed is not None:
                    on_gate_passed()
            if event.refused:
                refused = True
                try:
                    chan.close()
                except Exception:
                    pass
                return False
            raw = event.output
        for text, seq in batcher.add(raw):
            if not emit(text, seq):
                return False
        return True

    while True:
        if not should_continue():
            superseded = True
            break
        got_data = False
        for ready, receive in ((chan.recv_ready, chan.recv), (chan.recv_stderr_ready, chan.recv_stderr)):
            if ready():
                raw = receive(32768)
                if raw:
                    got_data = True
                    if not handle(raw):
                        superseded = not refused
                        break
        if superseded or refused:
            break
        for text, seq in batcher.flush():
            if not emit(text, seq):
                superseded = True
                break
        if superseded:
            break
        if not got_data:
            if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                break
            time.sleep(0.02)

    if not superseded and not refused:
        while chan.recv_ready():
            raw = chan.recv(32768)
            if not raw or not handle(raw):
                break
        while chan.recv_stderr_ready():
            raw = chan.recv_stderr(32768)
            if not raw or not handle(raw):
                break
        for text, seq in batcher.flush(final=True):
            if not emit(text, seq):
                superseded = True
                break
    if superseded:
        # Stop the abandoned command rather than leave it running, and so the
        # exit-status wait below returns at once instead of blocking.
        try:
            chan.close()
        except Exception:
            pass
    code = chan.recv_exit_status()
    return ReadResult(gate is None or gate.passed, refused or (gate is not None and not gate.passed and code == 97), superseded, code)
