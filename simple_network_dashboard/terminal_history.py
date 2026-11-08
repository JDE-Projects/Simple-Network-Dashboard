"""Plain-text, in-memory recovery history for one terminal run."""

from __future__ import annotations

from collections import deque
import json

from simple_network_dashboard import config


def _escaped_size(text: str) -> int:
    """Return the characters `text` takes inside a JSON string, without quotes."""
    return len(json.dumps(text)) - 2


def _keep_end(text: str, room: int) -> str:
    """Return the longest end of `text` whose escaped size fits in `room`."""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high) // 2
        if _escaped_size(text[middle:]) <= room:
            high = middle
        else:
            low = middle + 1
    return text[low:]


def trim_to_budget(terminals: list[dict], budget: int) -> list[dict]:
    """Trim history so the terminal list, measured as sent over the WebSocket,
    fits within `budget` characters shared evenly.

    Each share pays for the terminal's own fields, then the end of its
    unfinished line, then its newest complete lines. The fields are never
    dropped, because the page treats a terminal missing from the list as
    finished. They could only exceed the budget by themselves with thousands
    of terminals open, far past MAX_DEVICES."""
    if not terminals:
        return terminals
    share = (budget - 2) // len(terminals)  # 2 for the list's brackets
    for terminal in terminals:
        fields = {**terminal, "lines": [], "partial": ""}
        room = share - len(json.dumps(fields)) - 2  # 2 for the separator
        terminal["partial"] = _keep_end(terminal["partial"], room)
        used = _escaped_size(terminal["partial"])
        kept = 0
        for line in reversed(terminal["lines"]):
            used += len(json.dumps(line)) + 2
            if used > room:
                break
            kept += 1
        terminal["lines"] = terminal["lines"][len(terminal["lines"]) - kept:]
    return terminals


class TerminalHistory:
    """Keep the recoverable portion of terminal output without terminal state."""

    def __init__(self):
        self._lines = deque(maxlen=config.TERM_HISTORY_MAX_LINES)
        self._line = []
        self._position = 0
        self._state = "text"
        self._csi = ""
        self._fullscreen = False
        self._seq = 0

    def feed(self, text: str, seq: int) -> None:
        """Apply one browser-visible terminal batch to the recovery history."""
        self._seq = seq
        for char in text:
            self._feed_char(char)

    def snapshot(self) -> dict:
        """Return a copy suitable for a reconnecting browser."""
        return {
            "lines": list(self._lines),
            "partial": "".join(self._line),
            "fullscreen": self._fullscreen,
            "seq": self._seq,
        }

    def _feed_char(self, char: str) -> None:
        if self._state == "text":
            if char == "\x1b":
                self._state = "escape"
            elif not self._fullscreen:
                self._write_char(char)
            return
        if self._state == "escape":
            if char == "[":
                self._state, self._csi = "csi", ""
            elif char == "]":
                self._state = "osc"
            elif char in "PX^_":
                self._state = "string"
            elif char in "()":
                self._state = "charset"
            else:
                self._state = "text"
            return
        if self._state == "charset":
            self._state = "text"
            return
        if self._state == "csi":
            self._csi += char
            if "@" <= char <= "~":
                self._handle_csi(self._csi)
                self._state = "text"
            return
        if self._state == "osc":
            if char == "\a":
                self._state = "text"
            elif char == "\x1b":
                self._state = "osc_escape"
            return
        if self._state == "string":
            if char == "\x1b":
                self._state = "string_escape"
            return
        if self._state in ("osc_escape", "string_escape"):
            if char == "\\":
                self._state = "text"
            elif char != "\x1b":
                self._state = "osc" if self._state == "osc_escape" else "string"

    def _handle_csi(self, sequence: str) -> None:
        final = sequence[-1]
        params = sequence[:-1]
        if final == "K" and params in ("", "0") and not self._fullscreen:
            del self._line[self._position:]
            return
        if final not in "hl" or not params.startswith("?"):
            return
        modes = params[1:].split(";")
        if {"47", "1047", "1049"}.intersection(modes):
            self._fullscreen = final == "h"

    def _write_char(self, char: str) -> None:
        if char == "\n":
            self._lines.append("".join(self._line))
            self._line.clear()
            self._position = 0
        elif char == "\r":
            self._position = 0
        elif char == "\b":
            self._position = max(0, self._position - 1)
        elif char == "\t" or (ord(char) >= 32 and not 0x7F <= ord(char) <= 0x9F):
            if self._position < len(self._line):
                self._line[self._position] = char
                self._position += 1
            elif len(self._line) < config.TERM_HISTORY_MAX_LINE_CHARS:
                self._line.extend(" " * (self._position - len(self._line)))
                self._line.append(char)
                self._position += 1
