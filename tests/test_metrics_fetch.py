"""Tests for fetch_metrics's safe-resolution and connection behavior.

Covers the malformed-host guard, IP-literal validation, hostname resolution
via the event loop's async resolver, and the requirement that the HTTP
client connects to the validated IP (never the original hostname) and never
follows redirects.

DNS is faked by monkeypatching asyncio's BaseEventLoop.getaddrinfo (the
method fetch_metrics calls through loop.getaddrinfo). The HTTP layer is
faked with httpx.MockTransport, injected by monkeypatching httpx.AsyncClient
to a partial that always supplies our transport; fetch_metrics's production
code is unchanged and never takes a transport argument.
"""

import asyncio
import functools
import socket

import httpx

import metrics_poller
from config import METRICS_MAX_RESPONSE_BYTES
from metrics_poller import fetch_metrics


NODE_EXPORTER_SAMPLE = 'node_load1 0.5\n'


class _AsyncIteratorStream(httpx.AsyncByteStream):
    def __init__(self, iterator):
        self._iterator = iterator

    async def __aiter__(self):
        async for chunk in self._iterator:
            yield chunk

    async def aclose(self):
        await self._iterator.aclose()


def _install_mock_transport(monkeypatch, handler):
    """Make every httpx.AsyncClient created by fetch_metrics use `handler`
    as its transport, and return the MockTransport so tests can assert
    on call counts."""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=transport)
    )
    return transport


def _no_call_handler(calls):
    def handler(request):
        calls.append(request)
        raise AssertionError("HTTP should not have been called")
    return handler


def test_malformed_host_returns_error_with_no_dns_or_http(monkeypatch):
    calls = []
    _install_mock_transport(monkeypatch, _no_call_handler(calls))

    def fail_getaddrinfo(self, *args, **kwargs):
        raise AssertionError("DNS should not have been called")
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", fail_getaddrinfo)

    result = asyncio.run(fetch_metrics("bad host"))

    assert result == {"error": "invalid host"}
    assert calls == []


def test_rejected_ip_literal_returns_blocked_with_no_http(monkeypatch):
    calls = []
    _install_mock_transport(monkeypatch, _no_call_handler(calls))

    result = asyncio.run(fetch_metrics("127.0.0.1"))

    assert result == {"error": "blocked"}
    assert calls == []


def test_allowed_ip_literal_is_fetched_and_parsed(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=NODE_EXPORTER_SAMPLE)

    _install_mock_transport(monkeypatch, handler)

    result = asyncio.run(fetch_metrics("10.0.0.4"))

    assert "error" not in result
    assert len(calls) == 1
    assert calls[0].url.host == "10.0.0.4"


def test_response_exactly_at_size_cap_is_parsed(monkeypatch):
    body = NODE_EXPORTER_SAMPLE.encode()
    body += b"#" * (METRICS_MAX_RESPONSE_BYTES - len(body))

    def handler(request):
        return httpx.Response(200, content=body)

    _install_mock_transport(monkeypatch, handler)

    result = asyncio.run(fetch_metrics("10.0.0.4"))

    assert result["load1"] == 0.5


def test_response_over_size_cap_is_rejected_with_content_length(monkeypatch):
    body = b"x" * (METRICS_MAX_RESPONSE_BYTES + 1)

    def handler(request):
        return httpx.Response(200, headers={"Content-Length": str(len(body))}, content=body)

    _install_mock_transport(monkeypatch, handler)

    result = asyncio.run(fetch_metrics("10.0.0.4"))

    assert result == {"error": "response too large"}


def test_response_over_size_cap_is_rejected_without_content_length(monkeypatch):
    async def body():
        yield b"x" * (METRICS_MAX_RESPONSE_BYTES + 1)

    def handler(request):
        return httpx.Response(200, stream=_AsyncIteratorStream(body()))

    _install_mock_transport(monkeypatch, handler)

    result = asyncio.run(fetch_metrics("10.0.0.4"))

    assert result == {"error": "response too large"}


def test_content_length_over_size_cap_is_rejected_without_reading_body(monkeypatch):
    body_read = False

    async def body():
        nonlocal body_read
        body_read = True
        yield b"x"

    def handler(request):
        return httpx.Response(
            200,
            headers={"Content-Length": str(METRICS_MAX_RESPONSE_BYTES + 1)},
            stream=_AsyncIteratorStream(body()),
        )

    _install_mock_transport(monkeypatch, handler)

    result = asyncio.run(fetch_metrics("10.0.0.4"))

    assert result == {"error": "response too large"}
    assert not body_read


def test_slow_stream_hits_fetch_deadline(monkeypatch):
    async def body():
        while True:
            await asyncio.sleep(1)
            yield b"x"

    def handler(request):
        return httpx.Response(200, stream=_AsyncIteratorStream(body()))

    _install_mock_transport(monkeypatch, handler)
    monkeypatch.setattr(metrics_poller, "METRICS_FETCH_DEADLINE", 0.01)

    result = asyncio.run(fetch_metrics("10.0.0.4"))

    assert result == {"error": "timeout"}


def test_hostname_resolving_to_allowed_address_connects_to_ip_not_name(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=NODE_EXPORTER_SAMPLE)

    _install_mock_transport(monkeypatch, handler)

    async def fake_getaddrinfo(self, host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.4", port))]
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)

    result = asyncio.run(fetch_metrics("networkpi5"))

    assert "error" not in result
    assert len(calls) == 1
    assert calls[0].url.host == "10.0.0.4"


def test_hostname_resolving_to_rejected_address_returns_blocked_with_no_http(monkeypatch):
    calls = []
    _install_mock_transport(monkeypatch, _no_call_handler(calls))

    async def fake_getaddrinfo(self, host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)

    result = asyncio.run(fetch_metrics("networkpi5"))

    assert result == {"error": "blocked"}
    assert calls == []


def test_dns_resolution_failure_returns_dns_error_with_no_http(monkeypatch):
    calls = []
    _install_mock_transport(monkeypatch, _no_call_handler(calls))

    async def fake_getaddrinfo(self, host, port, *args, **kwargs):
        raise socket.gaierror("resolution failed")
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)

    result = asyncio.run(fetch_metrics("networkpi5"))

    assert result == {"error": "dns"}
    assert calls == []


def test_same_hostname_is_re_resolved_on_each_call(monkeypatch):
    """Two successive fetch_metrics calls for the same hostname must each
    perform a fresh DNS lookup, so a changed address is picked up rather
    than reused from a prior call."""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=NODE_EXPORTER_SAMPLE)

    _install_mock_transport(monkeypatch, handler)

    addresses = iter(["10.0.0.4", "10.0.0.5"])

    async def fake_getaddrinfo(self, host, port, *args, **kwargs):
        addr = next(addresses)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port))]
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)

    result_1 = asyncio.run(fetch_metrics("networkpi5"))
    result_2 = asyncio.run(fetch_metrics("networkpi5"))

    assert "error" not in result_1
    assert "error" not in result_2
    assert len(calls) == 2
    assert calls[0].url.host == "10.0.0.4"
    assert calls[1].url.host == "10.0.0.5"


def test_redirect_is_not_followed(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "http://10.0.0.5/metrics"})

    _install_mock_transport(monkeypatch, handler)

    result = asyncio.run(fetch_metrics("10.0.0.4"))

    assert "error" in result
    assert len(calls) == 1
