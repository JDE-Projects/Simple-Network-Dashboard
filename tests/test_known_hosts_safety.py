"""Safety checks for approved SSH host-key storage failures and atomic saves."""

import os

import pytest

import ssh_manager


_UNREADABLE_MESSAGE = (
    "The approved host keys file could not be read, so nothing was changed. "
    "Check the server log."
)


def _manager(debug_lines=None):
    async def _noop(_message):
        pass

    return ssh_manager.SSHManager(_noop, (debug_lines if debug_lines is not None else lambda _text: None))


def _key():
    return ssh_manager.paramiko.RSAKey.generate(1024)


def _pending(manager, key=None):
    key = key or _key()
    device_id = "device-1"
    owner = "browser-1"
    session = ssh_manager._Session(
        {"host": "host-one", "username": "user"}, "password", owner, device_id
    )
    session.state = "host_key_pending"
    session.generation = 1
    manager.sessions[device_id] = session
    manager._pending[device_id] = ssh_manager._PendingHostKey(
        "host-one", key, "approval-code", owner, 1
    )
    return device_id, owner, session


def _write_hosts(path, entries):
    host_keys = ssh_manager.paramiko.HostKeys()
    for host, key in entries:
        host_keys.add(host, key.get_name(), key)
    host_keys.save(str(path))


def test_unreadable_existing_file_blocks_trust_without_overwrite(tmp_path, monkeypatch):
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_bytes(b"approved host keys\n")
    monkeypatch.setattr(ssh_manager, "KNOWN_HOSTS_FILE", str(known_hosts))
    monkeypatch.setattr(
        ssh_manager.paramiko.HostKeys,
        "load",
        lambda *_args: (_ for _ in ()).throw(PermissionError("secret permission detail")),
    )
    debug_lines = []
    manager = _manager(debug_lines.append)
    device_id, owner, _session = _pending(manager)

    result = manager.trust_host_key(device_id, "approval-code", owner)

    assert result == {"ok": False, "error": _UNREADABLE_MESSAGE}
    assert known_hosts.read_bytes() == b"approved host keys\n"
    assert debug_lines
    assert "secret permission detail" not in result["error"]


def test_unreadable_existing_file_blocks_forget_without_overwrite(tmp_path, monkeypatch):
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_bytes(b"approved host keys\n")
    monkeypatch.setattr(ssh_manager, "KNOWN_HOSTS_FILE", str(known_hosts))
    monkeypatch.setattr(
        ssh_manager.paramiko.HostKeys,
        "load",
        lambda *_args: (_ for _ in ()).throw(PermissionError("secret permission detail")),
    )
    debug_lines = []

    result = _manager(debug_lines.append).forget_host_key("host-one")

    assert result == {"ok": False, "error": _UNREADABLE_MESSAGE}
    assert known_hosts.read_bytes() == b"approved host keys\n"
    assert debug_lines
    assert "secret permission detail" not in result["error"]


def test_trust_failure_tears_down_pending_session(tmp_path, monkeypatch):
    monkeypatch.setattr(ssh_manager, "KNOWN_HOSTS_FILE", str(tmp_path / "known_hosts"))
    monkeypatch.setattr(
        ssh_manager,
        "_save_known_hosts",
        lambda _host_keys: (_ for _ in ()).throw(OSError("save failed")),
    )
    manager = _manager()
    device_id, owner, session = _pending(manager)

    result = manager.trust_host_key(device_id, "approval-code", owner)

    assert result == {"ok": False, "error": "Could not save the host key."}
    assert device_id not in manager._pending
    assert device_id not in manager.sessions
    assert session.password is None


def test_trust_failure_does_not_close_replacement_session(tmp_path, monkeypatch):
    monkeypatch.setattr(ssh_manager, "KNOWN_HOSTS_FILE", str(tmp_path / "known_hosts"))
    manager = _manager()
    device_id, owner, original_session = _pending(manager)
    replacement_key = _key()
    replacement_session = ssh_manager._Session(
        {"host": "host-one", "username": "user"}, "replacement-password", owner, device_id
    )
    replacement_session.state = "host_key_pending"
    replacement_session.generation = 2
    replacement_pending = ssh_manager._PendingHostKey(
        "host-one", replacement_key, "replacement-code", owner, 2
    )

    def fail_after_replacement(_host_keys):
        manager.sessions[device_id] = replacement_session
        manager._pending[device_id] = replacement_pending
        raise OSError("save failed")

    monkeypatch.setattr(ssh_manager, "_save_known_hosts", fail_after_replacement)

    result = manager.trust_host_key(device_id, "approval-code", owner)

    assert result == {"ok": False, "error": "Could not save the host key."}
    assert manager.sessions[device_id] is replacement_session
    assert manager._pending[device_id] is replacement_pending
    assert original_session.password == "password"
    assert replacement_session.password == "replacement-password"


def test_trust_missing_file_creates_it_and_success_consumes_pending(tmp_path, monkeypatch):
    known_hosts = tmp_path / "known_hosts"
    monkeypatch.setattr(ssh_manager, "KNOWN_HOSTS_FILE", str(known_hosts))
    manager = _manager()
    device_id, owner, _session = _pending(manager)

    result = manager.trust_host_key(device_id, "approval-code", owner)

    assert result["ok"] is True
    assert known_hosts.exists()
    assert ssh_manager._load_known_hosts().lookup("host-one")
    assert device_id not in manager._pending


def test_trust_and_forget_keep_unrelated_host_entries(tmp_path, monkeypatch):
    known_hosts = tmp_path / "known_hosts"
    unrelated_key = _key()
    _write_hosts(known_hosts, [("unrelated", unrelated_key)])
    monkeypatch.setattr(ssh_manager, "KNOWN_HOSTS_FILE", str(known_hosts))
    manager = _manager()
    device_id, owner, _session = _pending(manager)

    assert manager.trust_host_key(device_id, "approval-code", owner)["ok"] is True
    assert manager.forget_host_key("host-one") == {"ok": True}

    loaded = ssh_manager._load_known_hosts()
    assert loaded.lookup("unrelated")
    assert not loaded.lookup("host-one")


def test_get_host_key_reports_unreadable_file_and_logs(tmp_path, monkeypatch):
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("approved")
    monkeypatch.setattr(ssh_manager, "KNOWN_HOSTS_FILE", str(known_hosts))
    monkeypatch.setattr(
        ssh_manager.paramiko.HostKeys,
        "load",
        lambda *_args: (_ for _ in ()).throw(PermissionError("secret permission detail")),
    )
    debug_lines = []

    result = _manager(debug_lines.append).get_host_key("host-one")

    assert result == {
        "ok": False,
        "error": "The approved host keys file could not be read. Check the server log.",
    }
    assert debug_lines


def test_connect_reports_unreadable_file_without_host_key_prompt(tmp_path, monkeypatch):
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("approved")
    monkeypatch.setattr(ssh_manager, "KNOWN_HOSTS_FILE", str(known_hosts))

    class Client:
        def load_host_keys(self, _path):
            raise PermissionError("secret permission detail")

    monkeypatch.setattr(ssh_manager.paramiko, "SSHClient", Client)
    debug_lines = []
    manager = _manager(debug_lines.append)

    result = manager.connect(
        "device-1", "password", {"host": "host-one", "username": "user"}, "browser-1"
    )

    assert result == {
        "ok": False,
        "error": "Could not connect: the approved host keys file could not be read. Check the server log.",
    }
    assert "host_key_unknown" not in result
    assert "secret permission detail" not in result["error"]
    assert debug_lines


def _assert_save_failure_preserves_old_file(tmp_path, monkeypatch, inject_failure):
    known_hosts = tmp_path / "known_hosts"
    _write_hosts(known_hosts, [("old-host", _key())])
    before = known_hosts.read_bytes()
    monkeypatch.setattr(ssh_manager, "KNOWN_HOSTS_FILE", str(known_hosts))
    inject_failure()

    with pytest.raises(OSError):
        ssh_manager._save_known_hosts(ssh_manager.paramiko.HostKeys())

    assert known_hosts.read_bytes() == before
    assert not list(tmp_path.glob(".known_hosts-*.tmp"))


def test_paramiko_save_failure_leaves_old_file_and_no_temp(tmp_path, monkeypatch):
    def inject_failure():
        monkeypatch.setattr(
            ssh_manager.paramiko.HostKeys,
            "save",
            lambda *_args: (_ for _ in ()).throw(OSError("save failure")),
        )

    _assert_save_failure_preserves_old_file(tmp_path, monkeypatch, inject_failure)


def test_fsync_failure_leaves_old_file_and_no_temp(tmp_path, monkeypatch):
    def inject_failure():
        monkeypatch.setattr(
            ssh_manager.os,
            "fsync",
            lambda _fd: (_ for _ in ()).throw(OSError("fsync failure")),
        )

    _assert_save_failure_preserves_old_file(tmp_path, monkeypatch, inject_failure)


def test_replace_failure_leaves_old_file_and_no_temp(tmp_path, monkeypatch):
    def inject_failure():
        monkeypatch.setattr(
            ssh_manager.os,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("replace failure")),
        )

    _assert_save_failure_preserves_old_file(tmp_path, monkeypatch, inject_failure)


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX file mode")
def test_saved_known_hosts_file_mode_is_private(tmp_path, monkeypatch):
    known_hosts = tmp_path / "known_hosts"
    monkeypatch.setattr(ssh_manager, "KNOWN_HOSTS_FILE", str(known_hosts))

    ssh_manager._save_known_hosts(ssh_manager.paramiko.HostKeys())

    assert known_hosts.stat().st_mode & 0o777 == 0o600
