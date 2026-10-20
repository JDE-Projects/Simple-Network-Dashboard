"""Tests for output draining in `_exec`: reading
the raw channel, interleaving stdout/stderr as they arrive, incremental UTF-8
decoding, and per-stream line splitting across chunks.

Reuses the scripted `_FakeChannel`/`_FakeClient` from
tests/test_session_enforcement.py. Nothing here touches the network."""

import ssh_manager
from tests.test_session_enforcement import _FakeChannel, _FakeClient


def _mgr():
    async def _noop(_msg):
        pass
    return ssh_manager.SSHManager(_noop, lambda _t: None)


def _device(device_id="d1"):
    return {"id": device_id, "host": "10.0.0.9", "username": "pi"}


def _connected_session(mgr, device, owner="owner-1", device_id="d1", generation=1):
    sess = ssh_manager._Session(device, "pw", owner, device_id)
    sess.state = "connected"
    sess.generation = generation
    mgr.sessions[device_id] = sess
    return sess


def _run(mgr, sess, channel, cmd="echo hi", label="Custom command", feed_sudo=False,
         sudo_prompt=None):
    sess.client = _FakeClient(channel)
    emitted = []
    events = []

    def fake_log(did, text, level="out", owner=None, cmd_id=None, gen=None):
        emitted.append((level, text))

    def fake_status(*a, **k):
        events.append(("status",) + a)

    mgr._log = fake_log
    mgr._status = fake_status
    mgr._exec(sess, cmd, label, feed_sudo, sudo_prompt=sudo_prompt)
    return emitted


def test_interleaved_stdout_and_stderr_both_emitted_at_correct_levels():
    mgr = _mgr()
    device = _device()
    sess = _connected_session(mgr, device)

    channel = _FakeChannel([
        ("out", b"first out\n"),
        ("err", b"first err\n"),
        ("out", b"second out\n"),
    ], exit_code=0)

    emitted = _run(mgr, sess, channel)

    assert ("out", "first out") in emitted
    assert ("err", "first err") in emitted
    assert ("out", "second out") in emitted
    assert emitted[-1] == ("ok", "✓ Custom command finished.")


def test_stderr_produced_before_exit_is_emitted_during_the_run_not_withheld():
    mgr = _mgr()
    device = _device()
    sess = _connected_session(mgr, device)

    channel = _FakeChannel([
        ("err", b"warning while running\n"),
        ("out", b"still running\n"),
    ], exit_code=0)

    emitted = _run(mgr, sess, channel)
    lines = [t for _, t in emitted]

    assert lines.index("warning while running") < lines.index("still running")
    assert emitted[-1][0] == "ok"


def test_multibyte_utf8_character_split_across_chunks_decodes_correctly():
    mgr = _mgr()
    device = _device()
    sess = _connected_session(mgr, device)

    text = "café\n".encode("utf-8")
    # Split the two-byte "é" (0xC3 0xA9) across two chunks.
    split_at = text.index(b"\xc3") + 1
    chunk1, chunk2 = text[:split_at], text[split_at:]

    channel = _FakeChannel([("out", chunk1), ("out", chunk2)], exit_code=0)

    emitted = _run(mgr, sess, channel)
    assert ("out", "café") in emitted


def test_invalid_utf8_bytes_become_replacement_character_without_raising():
    mgr = _mgr()
    device = _device()
    sess = _connected_session(mgr, device)

    channel = _FakeChannel([("out", b"bad:\xff\xfe:end\n")], exit_code=0)

    emitted = _run(mgr, sess, channel)
    matches = [t for lvl, t in emitted if lvl == "out" and t.startswith("bad:")]
    assert len(matches) == 1
    assert "�" in matches[0]


def test_final_line_with_no_trailing_newline_is_emitted_before_summary():
    mgr = _mgr()
    device = _device()
    sess = _connected_session(mgr, device)

    channel = _FakeChannel([("out", b"no trailing newline")], exit_code=0)

    emitted = _run(mgr, sess, channel)
    assert ("out", "no trailing newline") in emitted
    assert emitted[-1][0] == "ok"
    assert emitted.index(("out", "no trailing newline")) < len(emitted) - 1


def test_output_ready_only_at_exit_is_still_drained_before_summary():
    mgr = _mgr()
    device = _device()
    sess = _connected_session(mgr, device)

    # The exit marker sits ahead of a line that only becomes available once
    # the remote process has already exited.
    channel = _FakeChannel([
        ("out", b"before exit\n"),
        ("exit", 0),
        ("out", b"after exit marker\n"),
    ])

    emitted = _run(mgr, sess, channel)
    lines = [t for _, t in emitted]
    assert "before exit" in lines
    assert "after exit marker" in lines
    assert emitted[-1][0] == "ok"


def test_cancellation_mid_stream_stops_output_and_filters_password_lines():
    mgr = _mgr()
    device = _device()
    sess = _connected_session(mgr, device)
    sess.password = "s3cret"

    calls = {"n": 0}

    def on_event(_stream):
        calls["n"] += 1
        # Simulate cancel() firing from another thread, between the first
        # and second chunks: it flags the session and closes the channel.
        if calls["n"] == 2:
            sess.cancelled = True
            sess.cancel_requested = True
            channel.close()

    channel = _FakeChannel([
        ("out", b"s3cret\n"),
        ("out", b"[sudo] password for pi: \n"),
        ("out", b"never gets here\n"),
    ], on_event=on_event)

    prompt = "__SND_SUDO_test__"
    emitted = _run(mgr, sess, channel, feed_sudo=True,
                   cmd=f"sudo -S -p {prompt} echo hi", sudo_prompt=prompt)

    texts = [t for _, t in emitted]
    assert "s3cret" not in texts
    assert not any(t.startswith("[sudo] password for") for t in texts)
    assert "never gets here" not in texts
    assert emitted[-1] == ("warn", "■ Custom command cancelled.")


def test_sudo_password_is_written_only_after_its_prompt_arrives():
    mgr = _mgr()
    sess = _connected_session(mgr, _device())
    prompt = "__SND_SUDO_1234abcd__"
    events_seen = {"count": 0}

    def on_event(_stream):
        events_seen["count"] += 1
        if events_seen["count"] == 1:
            assert channel.stdin_writes == []

    channel = _FakeChannel([
        ("out", b"before prompt\n"),
        ("out", prompt.encode() + b"after prompt\n"),
    ], on_event=on_event)

    emitted = _run(mgr, sess, channel, feed_sudo=True,
                   cmd="sudo command", sudo_prompt=prompt)

    assert channel.stdin_writes == ["pw\n"]
    assert ("out", "before prompt") in emitted
    assert ("out", "after prompt") in emitted


def test_sudo_prompt_split_across_chunks_sends_password_once():
    mgr = _mgr()
    sess = _connected_session(mgr, _device())
    prompt = "__SND_SUDO_1234abcd__"
    split_at = len(prompt) // 2
    channel = _FakeChannel([
        ("out", prompt[:split_at].encode()),
        ("out", prompt[split_at:].encode() + b"done\n"),
    ])

    emitted = _run(mgr, sess, channel, feed_sudo=True,
                   cmd="sudo command", sudo_prompt=prompt)

    assert channel.stdin_writes == ["pw\n"]
    assert ("out", "done") in emitted


def test_password_free_sudo_never_receives_a_password():
    mgr = _mgr()
    sess = _connected_session(mgr, _device())
    prompt = "__SND_SUDO_1234abcd__"
    channel = _FakeChannel([("out", b"command output\n")])

    emitted = _run(mgr, sess, channel, feed_sudo=True,
                   cmd="sudo command", sudo_prompt=prompt)

    assert channel.stdin_writes == []
    assert ("out", "command output") in emitted


def test_sudo_prompt_and_echoed_password_are_removed_from_output():
    mgr = _mgr()
    sess = _connected_session(mgr, _device())
    sess.password = "s3cret"
    prompt = "__SND_SUDO_1234abcd__"
    channel = _FakeChannel([("out", (prompt + "s3cret\n").encode())])

    emitted = _run(mgr, sess, channel, feed_sudo=True,
                   cmd="sudo command", sudo_prompt=prompt)

    assert channel.stdin_writes == ["s3cret\n"]
    assert not any(prompt in text or "s3cret" in text for _, text in emitted)


def test_second_sudo_prompt_closes_the_channel_and_reports_rejection():
    mgr = _mgr()
    sess = _connected_session(mgr, _device())
    prompt = "__SND_SUDO_1234abcd__"
    channel = _FakeChannel([("out", (prompt * 2).encode())])

    emitted = _run(mgr, sess, channel, feed_sudo=True,
                   cmd="sudo command", sudo_prompt=prompt)

    assert channel.stdin_writes == ["pw\n"]
    assert channel._closed is True
    assert emitted.count(("err", "sudo did not accept the password.")) == 1
    assert not any("cancelled" in text for _, text in emitted)
    assert emitted[-1] == ("err", "✗ Custom command exited with code 1.")


def test_run_command_uses_a_distinct_prompt_for_each_sudo_wrapping_form(monkeypatch):
    mgr = _mgr()
    sess = _connected_session(mgr, _device())
    captured = []

    def fake_exec(*args):
        captured.append(args)

    class _FakeThread:
        def __init__(self, *, target, args, daemon):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(mgr, "_exec", fake_exec)
    monkeypatch.setattr(ssh_manager.threading, "Thread", _FakeThread)

    assert mgr.run_command("d1", "sudo reboot", True) == {"ok": True}
    sess.busy = False
    assert mgr.run_command("d1", "echo root", True) == {"ok": True}

    first, second = captured
    first_prompt = first[-1]
    second_prompt = second[-1]
    assert first_prompt != second_prompt
    assert first[1] == f"sudo -S -p {first_prompt} reboot"
    assert second[1] == f"sudo -S -p {second_prompt} bash -c 'echo root'"


def test_high_volume_interleaved_output_is_emitted_with_no_loss_or_reordering():
    """There is no bounded-queue or flow-control mechanism in this code, so
    this only verifies that a large flood of interleaved stdout/stderr lines
    is emitted with no loss and no reordering within each stream, the
    current-session gate still holds, and the poll loop terminates. It is not
    a test of true flow control (there is none to test)."""
    mgr = _mgr()
    device = _device()
    sess = _connected_session(mgr, device)

    n_lines = 1000
    events = []
    for i in range(n_lines):
        events.append(("out", f"out {i}\n".encode()))
        events.append(("err", f"err {i}\n".encode()))

    channel = _FakeChannel(events, exit_code=0)

    emitted = _run(mgr, sess, channel)

    out_lines = [t for lvl, t in emitted if lvl == "out"]
    err_lines = [t for lvl, t in emitted if lvl == "err"]

    assert len(out_lines) == n_lines
    assert len(err_lines) == n_lines

    out_ns = [int(t.split()[1]) for t in out_lines]
    err_ns = [int(t.split()[1]) for t in err_lines]
    assert out_ns == sorted(out_ns)
    assert err_ns == sorted(err_ns)
    assert out_ns == list(range(n_lines))
    assert err_ns == list(range(n_lines))

    assert emitted[-1] == ("ok", "✓ Custom command finished.")
