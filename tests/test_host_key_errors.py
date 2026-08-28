"""Tests that host-key trust/forget failures return a plain-language error and
keep the raw exception text out of the browser reply, routing it to the debug
log writer instead."""

import ssh_manager


def _mgr(debug=None):
    async def _noop(_msg):
        pass
    return ssh_manager.SSHManager(_noop, debug if debug is not None else (lambda _t: None))


def test_trust_host_key_hides_raw_error(monkeypatch):
    def boom():
        raise RuntimeError("secret internals")

    monkeypatch.setattr(ssh_manager, "_load_known_hosts", boom)

    messages = []
    mgr = _mgr(messages.append)
    mgr._pending["dev1"] = ("10.0.0.9", object())

    result = mgr.trust_host_key("dev1")

    assert result == {"ok": False, "error": "Could not save the host key."}
    assert "detail" not in result
    assert "secret internals" not in result["error"]
    assert messages == ["trust_host_key failed: RuntimeError: secret internals"]


def test_trust_host_key_missing_pending_is_plain():
    result = _mgr().trust_host_key("nope")
    assert result == {"ok": False, "error": "No host key is waiting to be trusted."}


def test_forget_host_key_hides_raw_error(monkeypatch):
    def boom():
        raise RuntimeError("secret internals")

    monkeypatch.setattr(ssh_manager, "_load_known_hosts", boom)

    messages = []
    mgr = _mgr(messages.append)

    result = mgr.forget_host_key("10.0.0.9")

    assert result == {"ok": False, "error": "Could not forget the host key."}
    assert "detail" not in result
    assert "secret internals" not in result["error"]
    assert messages == ["forget_host_key failed: RuntimeError: secret internals"]
