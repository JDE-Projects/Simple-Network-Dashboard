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
    sess = ssh_manager._Session({"host": "10.0.0.9", "username": "pi"}, "pw", "owner-1", "dev1")
    sess.state      = "host_key_pending"
    sess.generation = 1
    mgr.sessions["dev1"] = sess
    mgr._pending["dev1"] = ssh_manager._PendingHostKey(
        "10.0.0.9", object(), "code123", "owner-1", 1)

    result = mgr.trust_host_key("dev1", "code123", "owner-1")

    assert result == {"ok": False, "error": "Could not save the host key."}
    assert "detail" not in result
    assert "secret internals" not in result["error"]
    assert messages == ["trust_host_key failed: RuntimeError: secret internals"]


def test_trust_host_key_missing_pending_is_plain():
    result = _mgr().trust_host_key("nope", "any-code", "owner-1")
    assert result == {"ok": False, "expired": True, "error": ssh_manager._EXPIRED_ERROR}


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
