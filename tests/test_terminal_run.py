"""Pure terminal runner coverage, including the sudo authentication gate."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

import config
from terminal_run import OutputBatcher, SudoGate, build_command, clamp_size, read_channel
from tests.test_session_enforcement import _FakeChannel


def test_clamp_size_uses_defaults_and_bounds():
    assert clamp_size(None, None) == (120, 30)
    assert clamp_size(1, 999) == (20, 200)
    assert clamp_size(700, 1) == (500, 5)


def test_batcher_flushes_by_time_size_and_final_without_merging():
    now = [0.0]
    batcher = OutputBatcher(lambda: now[0])
    assert batcher.add(b"one") == []
    now[0] = config.TERM_BATCH_INTERVAL
    assert batcher.flush() == [("one", 1)]
    assert batcher.add(b"x" * config.TERM_BATCH_MAX_CHARS) == [("x" * config.TERM_BATCH_MAX_CHARS, 2)]
    assert batcher.add(b"three") == []
    assert batcher.flush(final=True) == [("three", 3)]


def test_batcher_incrementally_decodes_utf8():
    batcher = OutputBatcher(lambda: 0)
    assert batcher.add("café".encode()[:-1]) == []
    assert batcher.add("café".encode()[-1:]) == []
    assert batcher.flush(final=True) == [("café", 1)]


@pytest.mark.parametrize("split", range(len(b"noiseSNDsudopromptABCpasswordSNDtermmarkXYZ") + 1))
def test_gate_hides_everything_until_mark_across_every_split(split):
    phrase, mark = "SNDsudopromptABC", "SNDtermmarkXYZ"
    hidden = b"wrapper" + phrase.encode() + b"password echo***Sorry, try again."
    data = hidden + mark.encode() + b"visible"
    gate = SudoGate(phrase, mark)
    first, second = data[:split], data[split:]
    one, two = gate.feed(first), gate.feed(second)
    assert one.output + two.output == b"visible"
    assert one.send_password + two.send_password == 1
    assert not one.refused and not two.refused


def test_gate_refuses_on_second_phrase_and_retains_after_mark_bytes():
    gate = SudoGate("SNDsudopromptA", "SNDtermmarkB")
    assert gate.feed(b"SNDsudopromptA").send_password
    assert gate.feed(b"junkSNDsudopromptA").refused
    gate = SudoGate("SNDsudopromptA", "SNDtermmarkB")
    assert gate.feed(b"SNDtermmarkBafter").output == b"after"


@pytest.mark.parametrize("raw", ["echo 'x'; echo y", "echo first\necho second", "a | b || c && d", "sudo echo 'x'; echo y"])
def test_build_command_quotes_commands_without_sudo(raw):
    command, phrase, mark = build_command(raw, False)
    assert (command, phrase, mark) == (raw, None, None)


@pytest.mark.parametrize("raw", ["echo 'x'; echo y", "echo first\necho second", "a | b || c && d"])
def test_sudo_wrapper_quotes_the_whole_command_as_one_unit(raw):
    command, phrase, mark = build_command(raw, True)
    assert shlex.split(shlex.split(command)[-1]) == ["sudo", "bash", "-c", raw]
    typed = "sudo " + raw
    command, phrase, mark = build_command(typed, True)
    assert shlex.split(command)[-1] == typed
    assert command.startswith(f"set +x 2>/dev/null; sudo -S -p {phrase} -v || exit 97; printf '%s' {mark}; exec bash -c ")


def test_reader_announces_the_gate_before_reading_further_output():
    order = []
    phrase, mark = "SNDsudopromptA", "SNDtermmarkB"
    chan = _FakeChannel([("out", phrase.encode() + mark.encode()), ("out", b"later")],
                        on_event=lambda _stream: order.append("recv"))
    read_channel(chan, SudoGate(phrase, mark), OutputBatcher(lambda: 0),
                 lambda _text, _seq: True, lambda: True, lambda: None,
                 lambda: order.append("start"))
    assert order[:3] == ["recv", "start", "recv"]


def test_reader_closes_an_abandoned_channel():
    chan = _FakeChannel([("out", b"x")])
    result = read_channel(chan, None, OutputBatcher(lambda: 0), lambda _text, _seq: True,
                          lambda: False, lambda: None)
    assert result.superseded and chan._closed


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="requires a Unix bash")
def test_sudo_wrapper_refusal_prevents_typed_command_and_acceptance_marks_once(tmp_path: Path):
    marker = tmp_path / "ran"
    fake = tmp_path / "sudo"
    fake.write_text("#!/bin/sh\n[ \"$1\" = -S ] && shift\n[ \"$1\" = -p ] && shift 2\n[ \"$1\" = -v ] && exit \"${FAKE_SUDO_V:-0}\"\nexec \"$@\"\n")
    fake.chmod(0o755)
    raw = f"sudo sh -c 'touch {marker}'; true\necho ignored | cat || true && true"
    command, phrase, mark = build_command(raw, True)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "FAKE_SUDO_V": "1"}
    refused = subprocess.run(["bash", "-c", command], env=env, capture_output=True, check=False)
    assert refused.returncode == 97 and not marker.exists()
    env["FAKE_SUDO_V"] = "0"
    accepted = subprocess.run(["bash", "-c", command], env=env, capture_output=True, check=False)
    assert accepted.stdout.count(mark.encode()) == 1 and marker.exists()
    traced = subprocess.run(["bash", "-x", "-c", command], env=env, capture_output=True, check=False)
    assert traced.stdout.count(mark.encode()) == 1 and mark.encode() not in traced.stderr
