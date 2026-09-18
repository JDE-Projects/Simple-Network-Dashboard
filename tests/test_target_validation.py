"""Tests for metrics_poller's metrics-target address validation.

Covers the reject-list address classifier (is_allowed_metrics_target) and
the malformed-host sanity check (is_valid_host_string). Does not test any
wiring into the polling loop; that lands in a later phase.
"""

import pytest

from metrics_poller import is_allowed_metrics_target, is_valid_host_string


# --- Rejected address classes (IPv4 and IPv6) -------------------------------

@pytest.mark.parametrize("address", ["127.0.0.1", "::1"])
def test_loopback_rejected(address):
    assert is_allowed_metrics_target(address) is False


@pytest.mark.parametrize("address", ["169.254.1.1", "fe80::1"])
def test_link_local_rejected(address):
    assert is_allowed_metrics_target(address) is False


@pytest.mark.parametrize("address", ["224.0.0.1", "ff02::1"])
def test_multicast_rejected(address):
    assert is_allowed_metrics_target(address) is False


@pytest.mark.parametrize("address", ["0.0.0.0", "::"])
def test_unspecified_rejected(address):
    assert is_allowed_metrics_target(address) is False


def test_reserved_rejected():
    assert is_allowed_metrics_target("240.0.0.1") is False


# --- Allowed address classes ------------------------------------------------

@pytest.mark.parametrize("address", ["192.168.1.10", "10.0.0.4"])
def test_private_ipv4_allowed(address):
    assert is_allowed_metrics_target(address) is True


def test_public_ipv4_allowed():
    assert is_allowed_metrics_target("8.8.8.8") is True


def test_ordinary_ipv6_allowed():
    assert is_allowed_metrics_target("2001:4860:4860::8888") is True


def test_cgnat_allowed():
    """100.64.0.0/10 (carrier-grade NAT) must be allowed regardless of how
    the installed Python version reports is_private/is_global for it."""
    assert is_allowed_metrics_target("100.64.0.1") is True


# --- Unparseable input -------------------------------------------------------

def test_unparseable_address_raises():
    with pytest.raises(ValueError):
        is_allowed_metrics_target("not-an-ip")


# --- Malformed host string check --------------------------------------------

def test_empty_host_rejected():
    assert is_valid_host_string("") is False


def test_whitespace_only_host_rejected():
    assert is_valid_host_string("   ") is False


def test_embedded_space_rejected():
    assert is_valid_host_string("network pi5") is False


def test_control_character_rejected():
    assert is_valid_host_string("networkpi5\x07") is False


def test_overlength_host_rejected():
    assert is_valid_host_string("a" * 254) is False


def test_normal_hostname_accepted():
    assert is_valid_host_string("networkpi5") is True


def test_normal_ip_string_accepted():
    assert is_valid_host_string("10.0.0.4") is True
