"""Tests for update-check error classification and endpoint failures."""

import asyncio
import errno
import json
import socket
import ssl
import urllib.error

import pytest

import main


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid", code, "error", {}, None)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_http_error(403), "GitHub is rate-limiting update checks from this network. Try again later."),
        (_http_error(404), "No published release was found."),
        (_http_error(503), "GitHub is having trouble on its end (HTTP 503)."),
        (_http_error(401), "GitHub returned an error (HTTP 401)."),
        (
            json.JSONDecodeError("bad JSON", "not-json", 0),
            "GitHub returned something unexpected. This often means a proxy or a guest wifi sign-in page answered instead.",
        ),
        (
            urllib.error.URLError(ssl.SSLCertVerificationError("bad certificate")),
            "GitHub's certificate could not be verified. This usually means antivirus or a network filter is inspecting HTTPS traffic.",
        ),
        (
            urllib.error.URLError(ssl.SSLEOFError("unexpected EOF")),
            "The secure connection was cut off during the handshake with GitHub.",
        ),
        (
            urllib.error.URLError(ssl.SSLZeroReturnError("zero return")),
            "The secure connection was cut off during the handshake with GitHub.",
        ),
        (
            urllib.error.URLError(ssl.SSLError("handshake failed")),
            "The secure connection to GitHub failed.",
        ),
        (
            urllib.error.URLError(socket.gaierror("DNS failed")),
            "The address for api.github.com could not be looked up. Check DNS or the internet connection.",
        ),
        (urllib.error.URLError(socket.timeout()), "GitHub didn't respond in time."),
        (urllib.error.URLError(TimeoutError()), "GitHub didn't respond in time."),
        (
            urllib.error.URLError(ConnectionRefusedError()),
            "The connection was refused or reset. A firewall or proxy may be blocking it.",
        ),
        (
            urllib.error.URLError(ConnectionResetError()),
            "The connection was refused or reset. A firewall or proxy may be blocking it.",
        ),
        (urllib.error.URLError(OSError(errno.ENETUNREACH, "unreachable")), "No network connection."),
        (urllib.error.URLError("other network problem"), "Couldn't reach GitHub. Check the internet connection."),
        (RuntimeError("unexpected"), "RuntimeError: unexpected"),
    ],
)
def test_update_error_reason_classifies_failures(exc, expected):
    assert main._update_error_reason(exc) == expected


def test_update_error_reason_truncates_unknown_error():
    assert main._update_error_reason(RuntimeError("x" * 200)) == "RuntimeError: " + "x" * 103 + "..."


def test_check_update_returns_reason_and_logs_failure(monkeypatch):
    failure = urllib.error.URLError(socket.timeout("timed out"))
    messages = []

    def raise_failure():
        raise failure

    monkeypatch.setattr(main, "_fetch_latest_version", raise_failure)
    monkeypatch.setattr(main, "_debug_write", messages.append)

    result = asyncio.run(main.check_update())

    assert result == {"ok": False, "reason": "GitHub didn't respond in time."}
    assert messages == ["check_update failed: URLError: <urlopen error timed out>"]


def test_check_update_returns_reason_when_debug_logging_fails(monkeypatch):
    failure = urllib.error.URLError(socket.timeout("timed out"))

    def raise_failure():
        raise failure

    def raise_logging_error(_message):
        raise OSError("debug log unavailable")

    monkeypatch.setattr(main, "_fetch_latest_version", raise_failure)
    monkeypatch.setattr(main, "_debug_write", raise_logging_error)

    result = asyncio.run(main.check_update())

    assert result == {"ok": False, "reason": "GitHub didn't respond in time."}
