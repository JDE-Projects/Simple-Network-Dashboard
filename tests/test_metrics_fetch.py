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
import pytest

import metrics_poller
from metrics_poller import fetch_metrics


NODE_EXPORTER_SAMPLE = 'node_load1 0.5\n'


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


def test_redirect_is_not_followed(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "http://10.0.0.5/metrics"})

    _install_mock_transport(monkeypatch, handler)

    result = asyncio.run(fetch_metrics("10.0.0.4"))

    assert "error" in result
    assert len(calls) == 1
