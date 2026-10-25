"""Terminal event ordering and raw channel draining through ``_exec``."""

from simple_network_dashboard import ssh_manager
from tests.test_session_enforcement import _FakeChannel, _FakeClient


def _manager():
    async def noop(_message):
        pass
    return ssh_manager.SSHManager(noop, lambda _text: None)


def _session(manager, channel):
    sess = ssh_manager._Session({"id": "d1", "host": "host", "username": "user"}, "pw", "owner", "d1")
    sess.state, sess.generation, sess.client, sess.run_id = "connected", 1, _FakeClient(channel), "run-1"
    manager.sessions["d1"] = sess
    return sess


def _run(channel, *, sudo=False, phrase="SNDsudopromptA", mark="SNDtermmarkB"):
    manager = _manager()
    sess = _session(manager, channel)
    messages, logs = [], []
    manager._push = messages.append
    manager._push_terminal = lambda message, should_continue: messages.append(message) or should_continue()
    manager._log = lambda _did, text, level="out", **_kwargs: logs.append((level, text))
    manager._status = lambda *_args, **_kwargs: None
    manager._exec(sess, "command", "Custom command", sudo, cmd_id="client-1", sudo_phrase=phrase,
                  sudo_mark=mark, cols=900, rows=1, raw_command_length=7, run_id="run-1")
    return messages, logs, sess


def test_partial_prompt_reaches_terminal_before_command_ends():
    messages, logs, _ = _run(_FakeChannel([("out", b"Continue? [Y/n] ")]))
    assert [message["type"] for message in messages] == ["term_start", "term_data", "term_end"]
    assert messages[1]["data"] == "Continue? [Y/n] "
    assert logs == [("cmd", "$ Custom command"), ("ok", "✓ Custom command finished.")]


def test_utf8_split_and_raw_ansi_are_one_raw_terminal_batch():
    text = "caf\u00e9\x1b[31m\n".encode()
    messages, _, _ = _run(_FakeChannel([("out", text[:-2]), ("out", text[-2:])]))
    assert messages[1]["data"] == "caf\u00e9\x1b[31m\n"
    assert messages[1]["seq"] == 1


def test_sudo_hides_authentication_and_orders_terminal_events():
    phrase, mark = "SNDsudopromptA", "SNDtermmarkB"
    channel = _FakeChannel([("out", b"wrapper" + phrase.encode()),
                            ("out", b"pw echo***Sorry" + mark.encode() + b"visible")])
    messages, logs, _ = _run(channel, sudo=True, phrase=phrase, mark=mark)
    assert channel.stdin_writes == ["pw\n"]
    assert [message["type"] for message in messages] == ["term_start", "term_data", "term_end"]
    assert messages[0]["cmd_id"] == "client-1"
    assert messages[0]["run_id"] == messages[1]["run_id"] == "run-1"
    assert messages[0]["cols"] == 500 and messages[0]["rows"] == 5
    assert messages[1]["data"] == "visible"
    assert logs == [("cmd", "$ Custom command"), ("ok", "✓ Custom command finished.")]


def test_sudo_refusal_and_early_exit_have_one_approved_end_line():
    phrase, mark = "SNDsudopromptA", "SNDtermmarkB"
    channel = _FakeChannel([("out", phrase.encode()), ("out", phrase.encode())], exit_code=1)
    messages, logs, _ = _run(channel, sudo=True, phrase=phrase, mark=mark)
    assert messages == [] and channel.stdin_writes == ["pw\n"]
    assert logs == [("cmd", "$ Custom command"), ("err", "✗ Custom command did not run: sudo did not accept the password.")]
    messages, logs, _ = _run(_FakeChannel([], exit_code=2), sudo=True, phrase=phrase, mark=mark)
    assert messages == [] and logs == [("cmd", "$ Custom command"), ("err", "✗ Custom command did not run.")]
    messages, logs, _ = _run(_FakeChannel([], exit_code=97), sudo=True, phrase=phrase, mark=mark)
    assert messages == [] and logs == [("cmd", "$ Custom command"), ("err", "✗ Custom command did not run: sudo did not accept the password.")]


def test_nopasswd_starts_without_writing_password_and_uses_pty():
    phrase, mark = "SNDsudopromptA", "SNDtermmarkB"
    channel = _FakeChannel([("out", mark.encode() + b"ok")])
    messages, logs, _ = _run(channel, sudo=True, phrase=phrase, mark=mark)
    assert channel.stdin_writes == []
    assert channel.pty_calls == [{"term": "xterm-256color", "width": 500, "height": 5}]
    assert channel.commands == ["command"]
    assert messages[1]["data"] == "ok" and logs[-1][0] == "ok"


def test_non_sudo_defaults_to_terminal_and_run_command_returns_matching_run_id(monkeypatch):
    manager = _manager()
    channel = _FakeChannel([("out", b"ok")])
    _session(manager, channel)

    class InlineThread:
        def __init__(self, target, args, daemon): self.target, self.args = target, args
        def start(self): self.target(*self.args)

    messages = []
    manager._push = messages.append
    manager._push_terminal = lambda message, should_continue: messages.append(message) or should_continue()
    monkeypatch.setattr(ssh_manager.threading, "Thread", InlineThread)
    result = manager.run_command("d1", "command", False, owner="owner", cmd_id="id")
    starts = [message for message in messages if message["type"] == "term_start"]
    assert result["ok"] and result["run_id"] == starts[0]["run_id"]
    assert channel.pty_calls == [{"term": "xterm-256color", "width": 120, "height": 30}]
