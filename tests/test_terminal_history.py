"""Recovery-history behavior for live terminal commands."""

import config
from terminal_history import TerminalHistory


def _snapshot_after(text, split):
    history = TerminalHistory()
    history.feed(text[:split], 1)
    history.feed(text[split:], 2)
    return history.snapshot()


def test_escape_sequences_are_removed_across_every_split_and_line_break():
    for sequence in ("\x1b[31m", "\x1b]title\a", "\x1b]title\x1b\\", "\x1bPtext\x1b\\",
                     "\x1bXtext\x1b\\", "\x1b^text\x1b\\", "\x1b_text\x1b\\", "\x1b(0"):
        text = f"before{sequence}after\n"
        for split in range(len(text) + 1):
            assert _snapshot_after(text, split)["lines"] == ["beforeafter"]
    text = "before\x1b]title\nmore\x1b\\after\n"
    assert _snapshot_after(text, text.index("\n") + 1)["lines"] == ["beforeafter"]


def test_carriage_return_erase_backspace_and_partial_line():
    history = TerminalHistory()
    history.feed("10%\r50%\r100%\nabc\rZ\x1b[K\nabc\bZ\nDo you want to continue? [Y/n] ", 4)
    assert history.snapshot() == {
        "lines": ["100%", "Z", "abZ"],
        "partial": "Do you want to continue? [Y/n] ",
        "fullscreen": False,
        "seq": 4,
    }


def test_history_caps_complete_lines_and_current_line():
    history = TerminalHistory()
    history.feed("".join(f"{index}\n" for index in range(config.TERM_HISTORY_MAX_LINES + 2)), 1)
    history.feed("x" * (config.TERM_HISTORY_MAX_LINE_CHARS + 10), 2)
    snapshot = history.snapshot()
    assert snapshot["lines"] == [str(index) for index in range(2, config.TERM_HISTORY_MAX_LINES + 2)]
    assert snapshot["partial"] == "x" * config.TERM_HISTORY_MAX_LINE_CHARS


def test_fullscreen_modes_exclude_output_across_every_split():
    for mode in ("47", "1047", "1049"):
        text = f"before\n\x1b[?{mode}hinside\n\x1b[?{mode}lafter\n"
        for split in range(len(text) + 1):
            snapshot = _snapshot_after(text, split)
            assert snapshot["lines"] == ["before", "after"]
            assert not snapshot["fullscreen"]


def test_seq_watermark_tracks_the_last_batch_even_without_printable_output():
    history = TerminalHistory()
    history.feed("visible", 3)
    history.feed("\x1b[31m", 9)
    assert history.snapshot()["seq"] == 9


def test_delete_and_c1_control_characters_are_dropped():
    history = TerminalHistory()
    history.feed("a\x7fb\x9bc\x85d\n", 1)
    assert history.snapshot()["lines"] == ["abcd"]


def test_trim_keeps_newest_lines_and_partial_within_shared_budget():
    from terminal_history import trim_to_budget
    terminals = [
        {"lines": ["old", "mid", "new"], "partial": "prompt "},
        {"lines": ["x" * 10] * 5, "partial": ""},
    ]
    trim_to_budget(terminals, 50)
    assert terminals[0]["lines"] == ["mid", "new"]
    assert terminals[0]["partial"] == "prompt "
    assert terminals[1]["lines"] == ["x" * 10]


def test_trim_counts_escaped_size_of_non_ascii_text():
    from terminal_history import trim_to_budget
    terminals = [{"lines": ["é" * 10, "é" * 10], "partial": ""}]
    trim_to_budget(terminals, 100)
    assert terminals[0]["lines"] == ["é" * 10]
