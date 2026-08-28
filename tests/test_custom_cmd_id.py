"""Tests that the SSH manager threads a caller-supplied cmd_id tag into the
broadcast message for custom-command echoes, so the frontend can correlate a
typed command to its echoed console line without relying on message order."""

import ssh_manager


def _mgr():
    async def _noop(_msg):
        pass
    return ssh_manager.SSHManager(_noop)


def test_log_includes_cmd_id_when_given():
    mgr = _mgr()
    captured = []
    mgr._push = captured.append

    mgr._log("dev1", "$ Custom command", "cmd", cmd_id="c123")

    assert len(captured) == 1
    assert captured[0]["cmd_id"] == "c123"


def test_log_omits_cmd_id_when_not_given():
    mgr = _mgr()
    captured = []
    mgr._push = captured.append

    mgr._log("dev1", "$ Custom command", "cmd")

    assert len(captured) == 1
    assert "cmd_id" not in captured[0]
