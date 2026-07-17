"""Tests for main._parse_port: CLI port parsing for the entry point."""

import pytest

from main import _parse_port


def test_default_when_no_args():
    assert _parse_port([]) == 3000


def test_port_flag_with_space():
    assert _parse_port(["--port", "3005"]) == 3005


def test_port_flag_with_equals():
    assert _parse_port(["--port=3005"]) == 3005


def test_non_numeric_raises_system_exit():
    with pytest.raises(SystemExit):
        _parse_port(["--port", "abc"])


def test_zero_raises_system_exit():
    with pytest.raises(SystemExit):
        _parse_port(["--port", "0"])


def test_above_max_raises_system_exit():
    with pytest.raises(SystemExit):
        _parse_port(["--port", "65536"])


def test_unrelated_args_ignored():
    assert _parse_port(["--reload", "--host", "0.0.0.0"]) == 3000


def test_unrelated_args_ignored_alongside_port():
    assert _parse_port(["--foo", "bar", "--port=3010", "--baz"]) == 3010
