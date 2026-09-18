"""Tests for the Task 8 Phase 3 host-key approval gate in
ssh_manager.SSHManager: the accept/reject binding on owner + one-time code +
the reserved session's generation, one-time consumption, expiry, and pending
cleanup on every teardown path.

`_Session.connect` and paramiko are never exercised for real. Nothing here
touches the network."""

import ssh_manager


class _FakeKey:
    """Stands in for a paramiko key object: just enough for _fp()."""
    def get_name(self):
        return "ssh-ed25519"

    def asbytes(self):
        return b"fake-key-bytes"


class _FakeClient:
    """Stands in for paramiko.SSHClient: just enough for _Session.close()."""
    def close(self):
        pass


def _mgr():
    async def _noop(_msg):
        pass
    return ssh_manager.SSHManager(_noop, lambda _t: None)


def _device(device_id="d1"):
    return {"id": device_id, "host": "10.0.0.9", "username": "pi"}


def _pending_session(mgr, device_id="d1", owner="owner-1", code="the-code", generation=1):
    """Install a session in "host_key_pending" state plus its matching
    pending record, exactly as connect() would leave them after an
    UnknownHostKey / BadHostKeyException."""
    sess = ssh_manager._Session(_device(device_id), "pw", owner, device_id)
    sess.state      = "host_key_pending"
    sess.generation = generation
    mgr.sessions[device_id] = sess
    mgr._pending[device_id] = ssh_manager._PendingHostKey(
        "10.0.0.9", _FakeKey(), code, owner, generation)
    return sess


def test_accept_with_wrong_code_is_rejected_and_leaves_pending_untouched():
    mgr = _mgr()
    _pending_session(mgr)

    result = mgr.trust_host_key("d1", "wrong-code", "owner-1")

    assert result == {"ok": False, "expired": True, "error": ssh_manager._EXPIRED_ERROR}
    assert "d1" in mgr._pending
    assert mgr.sessions["d1"].state == "host_key_pending"


def test_accept_from_a_different_owner_is_rejected_and_leaves_pending_untouched():
    mgr = _mgr()
    _pending_session(mgr, owner="owner-1", code="the-code")

    result = mgr.trust_host_key("d1", "the-code", "owner-2")

    assert result == {"ok": False, "expired": True, "error": ssh_manager._EXPIRED_ERROR}
    assert "d1" in mgr._pending


def test_accept_with_matching_owner_and_code_pins_the_key_and_is_one_time(monkeypatch):
    mgr = _mgr()
    _pending_session(mgr, owner="owner-1", code="the-code")

    saved = {}

    def fake_load():
        return ssh_manager.paramiko.HostKeys()

    def fake_save(hk):
        saved["hk"] = hk

    monkeypatch.setattr(ssh_manager, "_load_known_hosts", fake_load)
    monkeypatch.setattr(ssh_manager, "_save_known_hosts", fake_save)

    result = mgr.trust_host_key("d1", "the-code", "owner-1")

    assert result["ok"] is True
    assert result["host"] == "10.0.0.9"
    assert "d1" not in mgr._pending
    assert "hk" in saved

    # One-time: the same code cannot be replayed now that the pending is gone.
    result2 = mgr.trust_host_key("d1", "the-code", "owner-1")
    assert result2 == {"ok": False, "expired": True, "error": ssh_manager._EXPIRED_ERROR}


def test_reject_with_matching_owner_and_code_clears_pending_and_releases_reservation():
    mgr = _mgr()
    sess = _pending_session(mgr, owner="owner-1", code="the-code")
    sess.client = _FakeClient()

    statuses = []
    locks = []

    result = mgr.reject_host_key("d1", "the-code", "owner-1")

    assert result == {"ok": True}
    assert "d1" not in mgr._pending
    assert "d1" not in mgr.sessions

    # No leftover statuses/locks captured above since we didn't monkeypatch
    # broadcast helpers here; a second run below verifies the broadcast path.
    del statuses, locks


def test_reject_broadcasts_idle_and_unlock(monkeypatch):
    mgr = _mgr()
    sess = _pending_session(mgr, owner="owner-1", code="the-code")
    sess.client = _FakeClient()

    statuses = []
    locks = []
    monkeypatch.setattr(mgr, "_status", lambda did, state, owner=None, gen=None: statuses.append((did, state)))
    monkeypatch.setattr(mgr, "_lock", lambda did, locked: locks.append((did, locked)))

    result = mgr.reject_host_key("d1", "the-code", "owner-1")

    assert result == {"ok": True}
    assert ("d1", "idle") in statuses
    assert ("d1", False) in locks


def test_reject_with_wrong_code_or_owner_does_not_consume_the_pending():
    mgr = _mgr()
    _pending_session(mgr, owner="owner-1", code="the-code")

    result = mgr.reject_host_key("d1", "wrong-code", "owner-1")
    assert result == {"ok": False, "expired": True, "error": ssh_manager._EXPIRED_ERROR}
    assert "d1" in mgr._pending
    assert "d1" in mgr.sessions

    result2 = mgr.reject_host_key("d1", "the-code", "owner-2")
    assert result2 == {"ok": False, "expired": True, "error": ssh_manager._EXPIRED_ERROR}
    assert "d1" in mgr._pending
    assert "d1" in mgr.sessions


def test_expired_pending_is_rejected_on_accept(monkeypatch):
    mgr = _mgr()
    _pending_session(mgr, owner="owner-1", code="the-code")
    # Push the pending's creation time far enough into the past to have
    # expired without needing to fake the clock for the whole test run.
    mgr._pending["d1"].created_at -= (ssh_manager.HOST_KEY_PENDING_SECONDS + 1)

    result = mgr.trust_host_key("d1", "the-code", "owner-1")

    assert result == {"ok": False, "expired": True, "error": ssh_manager._EXPIRED_ERROR}
    # Still present until the sweep or a fresh connect clears it.
    assert "d1" in mgr._pending


def test_idle_sweep_clears_expired_pending_and_releases_the_reservation(monkeypatch):
    mgr = _mgr()
    sess = _pending_session(mgr, owner="owner-1", code="the-code")
    sess.client = _FakeClient()
    mgr._pending["d1"].created_at -= (ssh_manager.HOST_KEY_PENDING_SECONDS + 1)

    import time
    mgr._sweep_expired_host_key_prompts(time.monotonic())

    assert "d1" not in mgr._pending
    assert "d1" not in mgr.sessions


def test_disconnect_clears_a_pending_host_key_prompt():
    mgr = _mgr()
    sess = _pending_session(mgr, owner="owner-1", code="the-code")
    sess.client = _FakeClient()

    mgr.disconnect("d1", "owner-1")

    assert "d1" not in mgr._pending
    assert "d1" not in mgr.sessions


def test_close_clears_a_pending_host_key_prompt():
    mgr = _mgr()
    sess = _pending_session(mgr, owner="owner-1", code="the-code")
    sess.client = _FakeClient()

    mgr._close("d1", sess)

    assert "d1" not in mgr._pending
    assert "d1" not in mgr.sessions


def test_release_owner_clears_a_pending_host_key_prompt():
    mgr = _mgr()
    sess = _pending_session(mgr, owner="owner-1", code="the-code")
    sess.client = _FakeClient()

    mgr.release_owner("owner-1")

    assert "d1" not in mgr._pending
    assert "d1" not in mgr.sessions
