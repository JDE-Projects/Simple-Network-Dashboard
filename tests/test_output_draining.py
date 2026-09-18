"""Tests for the Task 8 Phase 4 output-draining rewrite of `_exec`: reading
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


def _run(mgr, sess, channel, cmd="echo hi", label="Custom command", feed_sudo=False):
    sess.client = _FakeClient(channel)
    emitted = []
    events = []

    def fake_log(did, text, level="out", owner=None, cmd_id=None, gen=None):
        emitted.append((level, text))

    def fake_status(*a, **k):
        events.append(("status",) + a)

    mgr._log = fake_log
    mgr._status = fake_status
    mgr._exec(sess, cmd, label, feed_sudo)
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

    emitted = _run(mgr, sess, channel, feed_sudo=True, cmd="sudo -S -p '' echo hi")

    texts = [t for _, t in emitted]
    assert "s3cret" not in texts
    assert not any(t.startswith("[sudo] password for") for t in texts)
    assert "never gets here" not in texts
    assert emitted[-1] == ("warn", "■ Custom command cancelled.")


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
