"""Session log and transcript behaviour, including the M1 regression.

File path: tests/test_transcript.py
"""

import time

import pytest
from terminus.transcript import (
    SessionLog,
    SessionTranscript,
    apply_erasures,
    clean_raw,
    clean_text,
)


# --------------------------------------------------------------------------
# Pure cleanup
# --------------------------------------------------------------------------
def test_backspace_removes_the_character():
    assert apply_erasures(b"abcX\x08\x08de") == b"abde"


def test_backspace_does_not_cross_a_newline():
    assert apply_erasures(b"line1\n\x08\x08abc") == b"line1\nabc"


def test_cursor_back_expands_to_backspaces():
    assert apply_erasures(b"abcdef\x1b[3D") == b"abc"


def test_erase_to_eol_is_dropped():
    assert apply_erasures(b"abc\x1b[K") == b"abc"
    assert apply_erasures(b"abc\x1b[0K") == b"abc"


def test_clean_raw_strips_ansi_and_normalises_newlines():
    assert clean_raw(b"\x1b[31mred\x1b[0m\r\ntext\r") == b"red\ntext\n"


def test_clean_raw_collapses_excess_newlines():
    assert clean_raw(b"a\n\n\n\n\nb") == b"a\n\nb"


def test_clean_raw_strips_osc():
    assert clean_raw(b"\x1b]0;title\x07prompt$ ") == b"prompt$ "


def test_clean_text_decodes_invalid_bytes():
    assert "\ufffd" in clean_text(b"ok \xff\xfe")


# --------------------------------------------------------------------------
# SessionTranscript
# --------------------------------------------------------------------------
def test_screen_is_replaceable_not_appended():
    tr = SessionTranscript()
    tr.commit("committed line\n")
    tr.set_screen("screen A")
    first, _ = tr.read()
    tr.set_screen("screen B")
    second, _ = tr.read()

    assert "screen A" in first
    assert "screen A" not in second  # replaced, not accumulated
    assert "screen B" in second
    assert second.count("committed line") == 1


def test_repeated_reads_are_stable():
    tr = SessionTranscript()
    tr.commit("output\n")
    tr.set_screen("prompt#")
    assert tr.read()[0] == tr.read()[0] == tr.read()[0]


def test_trim_drops_oldest_and_flags_truncation():
    tr = SessionTranscript(max_chars=200)
    for n in range(50):
        tr.commit(f"line {n:04d}\n")
    text, truncated = tr.read()
    assert truncated is True
    assert "line 0000" not in text
    assert "line 0049" in text


def test_read_limit_trims_to_a_line_boundary():
    tr = SessionTranscript()
    tr.commit("\n".join(f"line {n}" for n in range(500)) + "\n")
    text, truncated = tr.read(limit=200)
    assert truncated is True
    assert len(text) <= 200
    assert not text.startswith("ine")


def test_marks_record_offsets():
    tr = SessionTranscript()
    tr.commit("a" * 10)
    tr.add_mark("[Terminus AI] show version")
    marks = tr.marks()
    assert marks[0]["offset"] == 10
    assert "show version" in marks[0]["label"]


# --------------------------------------------------------------------------
# SessionLog — the M1 regression
# --------------------------------------------------------------------------
@pytest.fixture
def log(tmp_path):
    path = tmp_path / "session.log"
    handle = open(path, "ab", buffering=0)
    transcript = SessionTranscript()
    session_log = SessionLog(handle, transcript)
    yield session_log, transcript, path
    session_log.close()
    try:
        handle.close()
    except Exception:
        pass


def read(path):
    return path.read_text(encoding="utf-8", errors="replace")


def test_repeated_snapshot_does_not_duplicate(log):
    """Regression for M1: `/logs` snapshots every live session."""
    session_log, _, path = log
    session_log.feed(b"switch#show version\r\nCisco IOS 15.2\r\nswitch#")
    for _ in range(6):
        session_log.snapshot()
        time.sleep(0.02)

    content = read(path)
    assert content.count("Cisco IOS 15.2") == 1


def test_changing_screen_replaces_the_previous_one(log):
    """Regression for P1: provisional writes must overwrite, not stack."""
    session_log, _, path = log
    session_log.feed(b"first output\r\nswitch#")
    session_log.snapshot()
    session_log.feed(b"second output\r\nswitch#")
    session_log.snapshot()

    content = read(path)
    assert content.count("first output") == 1
    assert content.count("second output") == 1
    assert "\x00" not in content  # truncate() left no NUL padding


def test_markers_are_ordered_against_output(log):
    session_log, transcript, path = log
    session_log.write_marker(
        "────── [Terminus AI] read_only ──────\n$ show clock"
    )
    session_log.feed(b"show clock\r\n09:00:00 UTC\r\nswitch#")
    session_log.write_marker("────── [Terminus AI] ✔ ok ──────")
    session_log.snapshot()

    content = read(path)
    header = content.index("read_only")
    body = content.index("09:00:00 UTC")
    footer = content.index("✔ ok")
    assert header < body < footer
    assert len(transcript.marks()) == 2


def test_transcript_and_file_agree(log):
    session_log, transcript, path = log
    session_log.feed(b"line one\r\nline two\r\nswitch#")
    session_log.snapshot()

    text, _ = transcript.read()
    for fragment in ("line one", "line two"):
        assert fragment in text
        assert fragment in read(path)


def test_note_input_is_non_blocking(log):
    """M2: note_input must not wait on the worker's lock."""
    session_log, _, _ = log
    for _ in range(200):
        session_log.feed(b"x" * 4096)
    started = time.monotonic()
    session_log.note_input()
    assert time.monotonic() - started < 0.05


def test_bulk_mode_handles_flood(log):
    session_log, _, path = log
    for n in range(400):
        session_log.feed(f"flood line {n:04d}\r\n".encode())
    session_log.snapshot()

    content = read(path)
    assert "flood line 0399" in content
    assert content.count("flood line 0200") == 1


def test_close_survives_a_closed_tee(tmp_path):
    """H2: writing to a closed handle raises ValueError, not OSError."""
    path = tmp_path / "s.log"
    handle = open(path, "ab", buffering=0)
    session_log = SessionLog(handle, SessionTranscript())
    session_log.feed(b"data\r\nswitch#")
    time.sleep(0.05)
    handle.close()
    session_log.close()  # must not raise
    session_log.snapshot()  # must not raise


def test_no_tee_still_feeds_the_transcript():
    transcript = SessionTranscript()
    session_log = SessionLog(None, transcript)
    session_log.feed(b"output only in memory\r\nswitch#")
    session_log.snapshot()
    session_log.close()
    assert "output only in memory" in transcript.read()[0]
