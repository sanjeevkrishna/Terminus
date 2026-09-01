"""Capture, preflight, arbitration and batching against a fake channel.

File path: tests/test_executor.py
"""

import re
import threading
import time

import pytest
from terminus.ai import executor
from terminus.ai.executor import (
    STATUS_BUSY,
    STATUS_CANCELLED,
    STATUS_ERROR,
    STATUS_LOCKED,
    STATUS_OK,
    STATUS_SESSION_GONE,
    STATUS_SKIPPED,
    STATUS_TIMEOUT,
    STATUS_WRONG_MODE,
    run_batch,
    run_one,
)
from tests.conftest import make_item
from tests.fakes import FakeChannel

VERSION = "Cisco IOS Software, Version 15.2(4)E10\r\nuptime is 3 weeks"


def settle(sess, seconds=0.12):
    """Let the pump drain the initial prompt so preflight sees a clean idle."""
    time.sleep(seconds)


# --------------------------------------------------------------------------
# Single command
# --------------------------------------------------------------------------
def test_basic_capture(live_session):
    channel = FakeChannel(responses={"show version": VERSION})
    sess, _ = live_session(channel)
    settle(sess)

    result = run_one(sess, make_item("show version"))

    assert result.status == STATUS_OK
    assert "Version 15.2" in result.output
    assert "show version" not in result.output  # echo stripped
    assert "switch#" not in result.output  # prompt stripped
    assert result.bytes_read > 0
    assert result.elapsed > 0
    assert channel.sent == ["show version\r"]


def test_capture_slot_is_released(live_session):
    channel = FakeChannel(responses={"show clock": "09:00:00 UTC"})
    sess, _ = live_session(channel)
    settle(sess)
    run_one(sess, make_item("show clock"))
    assert sess["capture"] is None


def test_device_error_is_reported(live_session):
    channel = FakeChannel(responses={})  # unknown → error
    sess, _ = live_session(channel)
    settle(sess)

    result = run_one(sess, make_item("show frobnicate"))

    assert result.status == STATUS_ERROR
    assert "rejected" in result.detail
    assert "Invalid input" in result.output


def test_prompt_split_across_chunks(live_session):
    """The prompt arriving in two reads must still be detected."""
    channel = FakeChannel(
        responses={
            "show version": [
                (0.0, VERSION + "\r\n"),
                (0.05, "swi"),
                (0.05, "tch#"),
            ],
        }
    )
    sess, _ = live_session(channel)
    settle(sess)

    result = run_one(sess, make_item("show version"))

    assert result.status == STATUS_OK
    assert "Version 15.2" in result.output


def test_prompt_string_inside_output_does_not_end_capture_early(live_session):
    channel = FakeChannel(
        responses={
            "show archive": [
                (0.00, "  1: switch# configure terminal\r\n"),
                (0.15, "  2: switch(config)# interface Gi0/1\r\n"),
                (0.15, "  3: end\r\n"),
                (0.15, "switch#"),  # the real prompt terminates the capture
            ],
        }
    )
    sess, _ = live_session(channel)
    settle(sess)

    result = run_one(sess, make_item("show archive"))

    assert result.status == STATUS_OK
    assert "1: switch# configure terminal" in result.output
    assert "2: switch(config)# interface Gi0/1" in result.output
    assert "3: end" in result.output
    assert not result.output.rstrip().endswith("switch#")  # prompt consumed


def test_timeout_returns_partial_output_and_aborts(live_session):
    def never_finishes(channel, _command):
        channel.emit("partial line 1\r\n")
        channel.emit("partial line 2\r\n", delay=0.1)
        # no prompt, ever

    channel = FakeChannel(responses={"show hang": never_finishes})
    sess, _ = live_session(channel)
    settle(sess)

    result = run_one(sess, make_item("show hang"), timeout=0.5)

    assert result.status == STATUS_TIMEOUT
    assert "partial line 1" in result.output
    assert "incomplete" in result.detail
    assert "\x03" in channel.sent  # Ctrl-C sent on abandon


def test_cancel_mid_command(live_session):
    def slow(channel, _command):
        channel.emit("working...\r\n")
        channel.emit(f"done\r\n{channel.prompt}", delay=2.0)

    channel = FakeChannel(responses={"show slow": slow})
    sess, _ = live_session(channel)
    settle(sess)

    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()

    result = run_one(
        sess, make_item("show slow"), cancel_event=cancel, timeout=5.0
    )

    assert result.status == STATUS_CANCELLED
    assert "working" in result.output


def test_cancel_before_start_does_not_send(live_session):
    channel = FakeChannel(responses={"show version": VERSION})
    sess, _ = live_session(channel)
    settle(sess)

    cancel = threading.Event()
    cancel.set()
    result = run_one(sess, make_item("show version"), cancel_event=cancel)

    assert result.status == STATUS_CANCELLED
    assert channel.sent == []


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------
def test_busy_session_is_refused(live_session, monkeypatch):
    # Windows sleep granularity is ~15ms, so a 20ms chatter needs a generous
    monkeypatch.setattr(executor, "_PREFLIGHT_IDLE", 0.3)

    channel = FakeChannel(responses={})
    sess, _ = live_session(channel)
    settle(sess)

    stop = threading.Event()

    def chatter():
        while not stop.is_set():
            channel.emit("counter tick\r\n")
            time.sleep(0.02)

    noise = threading.Thread(target=chatter, daemon=True)
    noise.start()
    try:
        result = run_one(sess, make_item("show version"))
    finally:
        stop.set()
        noise.join(timeout=1.0)

    assert result.status == STATUS_BUSY
    assert channel.sent == []  # nothing was sent to a busy session


def test_config_mode_is_refused_and_never_auto_exited(live_session):
    channel = FakeChannel(prompt="switch(config)#", responses={})
    sess, _ = live_session(channel, base_prompt="switch")
    settle(sess)

    result = run_one(sess, make_item("show version"))

    assert result.status == STATUS_WRONG_MODE
    assert "configuration mode" in result.detail
    assert channel.sent == []  # no `end`, no command


def test_closed_channel_is_session_gone(live_session):
    channel = FakeChannel(responses={})
    sess, _ = live_session(channel)
    settle(sess)
    channel.closed = True

    result = run_one(sess, make_item("show version"))
    assert result.status == STATUS_SESSION_GONE


def test_missing_session_is_session_gone():
    assert (
        run_one(None, make_item("show version")).status == STATUS_SESSION_GONE
    )


# --------------------------------------------------------------------------
# Arbitration
# --------------------------------------------------------------------------
def test_second_command_cannot_interleave(live_session, monkeypatch):
    monkeypatch.setattr(executor, "_LOCK_TIMEOUT", 0.1)

    def slow(channel, _command):
        channel.emit(f"slow output\r\n{channel.prompt}", delay=0.6)

    channel = FakeChannel(responses={"show slow": slow})
    sess, _ = live_session(channel)
    settle(sess)

    results = {}

    def first():
        results["a"] = run_one(sess, make_item("show slow"), timeout=3.0)

    thread = threading.Thread(target=first)
    thread.start()
    time.sleep(0.15)
    results["b"] = run_one(sess, make_item("show slow"))
    thread.join(timeout=5.0)

    assert results["a"].status == STATUS_OK
    assert results["b"].status == STATUS_LOCKED


# --------------------------------------------------------------------------
# POSIX sentinel path
# --------------------------------------------------------------------------
def test_sentinel_path_on_linux(live_session):
    channel = FakeChannel(
        prompt="user@web-01:~$",
        posix=True,
        responses={"ls -la": "total 4\r\ndrwxr-xr-x 2 root"},
    )
    sess, _ = live_session(
        channel, device_type="linux", base_prompt="user@web-01"
    )
    settle(sess)

    result = run_one(sess, make_item("ls -la", device_type="linux"))

    assert result.status == STATUS_OK
    assert "total 4" in result.output
    assert "__TERMINUS_" not in result.output  # sentinel scrubbed
    assert re.search(r"; echo __TERMINUS_\w+__\r$", channel.sent[0])


# --------------------------------------------------------------------------
# Paging
# --------------------------------------------------------------------------
def test_paging_prompt_is_answered_and_flagged(live_session):
    def paged(channel, _command):
        channel.emit("page one content\r\n --More-- ")
        channel.emit(f"page two content\r\n{channel.prompt}", delay=0.15)

    channel = FakeChannel(responses={"show log": paged})
    sess, _ = live_session(channel)
    settle(sess)

    result = run_one(sess, make_item("show log"), timeout=3.0)

    assert result.paged is True
    assert "Paging was active" in result.detail
    assert " " in channel.sent  # space sent to advance
    assert "page two content" in result.output


# --------------------------------------------------------------------------
# Markers
# --------------------------------------------------------------------------
def test_markers_are_written_and_ordered(live_session):
    class RecordingLog:
        def __init__(self):
            self.markers = []

        def write_marker(self, text):
            self.markers.append(text)

    log = RecordingLog()
    channel = FakeChannel(responses={"show version": VERSION})
    sess, _ = live_session(channel, session_log=log)
    settle(sess)

    run_one(sess, make_item("show version"))

    assert len(log.markers) == 2
    assert "$ show version" in log.markers[0]
    assert "read_only" in log.markers[0]
    assert "ok" in log.markers[1]


def test_terminal_notify_receives_both_markers(live_session):
    channel = FakeChannel(responses={"show version": VERSION})
    sess, _ = live_session(channel)
    settle(sess)

    seen = []
    run_one(
        sess,
        make_item("show version"),
        notify=lambda sid, text: seen.append((sid, text)),
    )

    assert len(seen) == 2
    assert all(sid == "s1" for sid, _ in seen)
    assert "AI ▶ show version" in seen[0][1]
    assert "AI ✔" in seen[1][1]


def test_no_markers_when_preflight_refuses(live_session):
    channel = FakeChannel(prompt="switch(config)#", responses={})
    sess, _ = live_session(channel, base_prompt="switch")
    settle(sess)

    seen = []
    run_one(
        sess,
        make_item("show version"),
        notify=lambda sid, text: seen.append(text),
    )

    assert seen == []  # nothing announced, because nothing ran


# --------------------------------------------------------------------------
# Batching
# --------------------------------------------------------------------------
@pytest.fixture
def three_sessions(live_session):
    built = {}
    for index, (alias, host) in enumerate(
        [("S1", "core-sw1"), ("S2", "core-sw2"), ("S3", "core-sw3")], 1
    ):
        channel = FakeChannel(
            prompt=f"{host}#",
            responses={
                "show version": f"{host} running 15.2",
                "show clock": f"{host} 09:00:00",
            },
        )
        sess, _ = live_session(
            channel,
            session_id=f"s{index}",
            alias=alias,
            hostname=host,
            base_prompt=host,
        )
        built[f"s{index}"] = sess
    time.sleep(0.15)
    return built


def test_batch_runs_sessions_in_parallel(three_sessions):
    items = [
        make_item(
            "show version",
            alias=a,
            session_id=s,
            hostname=three_sessions[s]["hostname"],
        )
        for a, s in (("S1", "s1"), ("S2", "s2"), ("S3", "s3"))
    ]

    started = time.monotonic()
    results = run_batch(items, three_sessions.get)
    elapsed = time.monotonic() - started

    assert [r.status for r in results] == [STATUS_OK] * 3
    assert elapsed < 1.5  # parallel, not 3x serial
    assert {r.alias for r in results} == {"S1", "S2", "S3"}


def test_batch_preserves_plan_order(three_sessions):
    items = [
        make_item("show version", alias="S1", session_id="s1"),
        make_item("show version", alias="S2", session_id="s2"),
        make_item("show clock", alias="S1", session_id="s1"),
        make_item("show clock", alias="S2", session_id="s2"),
    ]
    results = run_batch(items, three_sessions.get)
    assert [(r.alias, r.command) for r in results] == [
        ("S1", "show version"),
        ("S2", "show version"),
        ("S1", "show clock"),
        ("S2", "show clock"),
    ]


def test_same_session_commands_run_sequentially(three_sessions):
    sess = three_sessions["s1"]
    channel = sess["channel"]
    items = [
        make_item("show version", session_id="s1"),
        make_item("show clock", session_id="s1"),
    ]

    results = run_batch(items, three_sessions.get)

    assert [r.status for r in results] == [STATUS_OK, STATUS_OK]
    assert channel.sent == ["show version\r", "show clock\r"]


def test_progress_callback_fires_per_command(three_sessions):
    items = [
        make_item("show version", alias="S1", session_id="s1"),
        make_item("show version", alias="S2", session_id="s2"),
    ]
    seen = []
    run_batch(items, three_sessions.get, progress=seen.append)
    assert len(seen) == 2
    assert {row["alias"] for row in seen} == {"S1", "S2"}
    assert all("elapsed" in row and "bytes" in row for row in seen)


def test_missing_session_reported_not_crashed(three_sessions):
    items = [
        make_item("show version", alias="S1", session_id="s1"),
        make_item("show version", alias="S9", session_id="gone"),
    ]
    results = run_batch(items, three_sessions.get)
    statuses = {r.alias: r.status for r in results}
    assert statuses["S1"] == STATUS_OK
    assert statuses["S9"] == STATUS_SESSION_GONE


def test_terminal_status_skips_remaining_on_that_session(three_sessions):
    """A gone session must not have its later commands attempted."""
    items = [
        make_item("show version", alias="S9", session_id="gone"),
        make_item("show clock", alias="S9", session_id="gone"),
        make_item("show version", alias="S1", session_id="s1"),
    ]
    results = run_batch(items, three_sessions.get)
    by_command = {(r.alias, r.command): r for r in results}
    assert by_command[("S9", "show version")].status == STATUS_SESSION_GONE
    assert by_command[("S9", "show clock")].status == STATUS_SKIPPED
    assert by_command[("S1", "show version")].status == STATUS_OK


def test_turn_budget_stops_further_commands(three_sessions):
    items = [
        make_item("show version", alias="S1", session_id="s1"),
        make_item("show clock", alias="S1", session_id="s1"),
    ]
    results = run_batch(items, three_sessions.get, turn_budget=10)
    assert results[0].status == STATUS_OK
    assert results[1].status == STATUS_SKIPPED
    assert "budget" in results[1].detail


def test_batch_cancel_skips_everything_pending(three_sessions):
    items = [
        make_item("show version", alias=a, session_id=s)
        for a, s in (("S1", "s1"), ("S2", "s2"), ("S3", "s3"))
    ]
    cancel = threading.Event()
    cancel.set()
    results = run_batch(items, three_sessions.get, cancel_event=cancel)
    assert all(r.status in (STATUS_CANCELLED, STATUS_SKIPPED) for r in results)


def test_empty_batch():
    assert run_batch([], lambda _sid: None) == []
