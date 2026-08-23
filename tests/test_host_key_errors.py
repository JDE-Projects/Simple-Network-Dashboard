"""Tests that host-key trust/forget failures return a plain-language error and
keep the raw exception text out of the browser reply."""

import main
import ssh_manager


def _mgr():
    async def _noop(_msg):
        pass
    return ssh_manager.SSHManager(_noop)


def test_trust_host_key_hides_raw_error(monkeypatch):
    def boom():
        raise RuntimeError("secret internals")

    monkeypatch.setattr(ssh_manager, "_load_known_hosts", boom)

    mgr = _mgr()
    mgr._pending["dev1"] = ("10.0.0.9", object())

    result = mgr.trust_host_key("dev1")

    assert result["ok"] is False
    assert result["error"] == "Could not save the host key."
    assert "secret internals" not in result["error"]
    assert result["detail"] == "trust_host_key failed: RuntimeError: secret internals"


def test_trust_host_key_missing_pending_is_plain():
    result = _mgr().trust_host_key("nope")
    assert result == {"ok": False, "error": "No host key is waiting to be trusted."}


def test_forget_host_key_hides_raw_error(monkeypatch):
    def boom():
        raise RuntimeError("secret internals")

    monkeypatch.setattr(ssh_manager, "_load_known_hosts", boom)

    result = _mgr().forget_host_key("10.0.0.9")

    assert result["ok"] is False
    assert result["error"] == "Could not forget the host key."
    assert "secret internals" not in result["error"]
    assert result["detail"] == "forget_host_key failed: RuntimeError: secret internals"


def test_log_and_strip_detail_logs_and_removes(monkeypatch):
    messages = []
    monkeypatch.setattr(main, "_debug_write", messages.append)

    result = main._log_and_strip_detail(
        {"ok": False, "error": "Could not save the host key.", "detail": "boom"}
    )

    assert result == {"ok": False, "error": "Could not save the host key."}
    assert "detail" not in result
    assert messages == ["boom"]


def test_log_and_strip_detail_no_detail_no_log(monkeypatch):
    messages = []
    monkeypatch.setattr(main, "_debug_write", messages.append)

    result = main._log_and_strip_detail({"ok": True, "host": "10.0.0.9"})

    assert result == {"ok": True, "host": "10.0.0.9"}
    assert messages == []
