"""Shared fixtures.

File path: tests/conftest.py
"""

import threading

import pytest
from terminus.ai import executor
from terminus.ai.policy import PlanItem
from terminus.transcript import SessionMonitor
from tests.fakes import Pump


@pytest.fixture
def live_session():
    """Yield ``start(channel, **kw) -> (sess, pump)`` and clean up after.

    Named distinctly rather than `live`: pytest resolves fixture names across all
    installed plugins, and a short generic name is liable to be shadowed.
    """
    pumps = []

    def start(channel, **kwargs):
        sess = make_session(channel, **kwargs)
        pump = Pump(sess)
        pump.start()
        pumps.append((pump, channel))
        return sess, pump

    yield start

    for pump, channel in pumps:
        pump.stop()
        channel.close()


@pytest.fixture(autouse=True)
def fast_timings(monkeypatch):
    """Shrink the real-time waits so the suite runs in seconds, not minutes."""
    monkeypatch.setattr(executor, "_PREFLIGHT_IDLE", 0.05)
    monkeypatch.setattr(executor, "_QUIET_PERIOD", 0.08)
    monkeypatch.setattr(executor, "_SETTLE_AFTER_SEND", 0.01)
    monkeypatch.setattr(executor, "_ABORT_SETTLE", 0.05)
    monkeypatch.setattr(executor, "_INTER_COMMAND_GAP", 0.01)
    monkeypatch.setattr(executor, "_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(executor, "DEFAULT_TIMEOUT", 3.0)
    monkeypatch.setattr(executor, "SLOW_TIMEOUT", 5.0)


def make_session(
    channel,
    *,
    session_id="s1",
    alias="S1",
    hostname="core-sw1",
    device_type="cisco_ios",
    base_prompt=None,
    session_log=None,
):
    """Build the session dict shape the executor expects."""
    return {
        "session_id": session_id,
        "alias": alias,
        "channel": channel,
        "monitor": SessionMonitor(),
        "exec_lock": threading.Lock(),
        "capture": None,
        "base_prompt": base_prompt
        if base_prompt is not None
        else channel.prompt.rstrip("#>$%]"),
        "hostname": hostname,
        "device_type": device_type,
        "session_log": session_log,
    }


def make_item(
    command,
    *,
    alias="S1",
    session_id="s1",
    hostname="core-sw1",
    device_type="cisco_ios",
    risk="read_only",
):
    return PlanItem(
        alias=alias,
        session_id=session_id,
        hostname=hostname,
        device_type=device_type,
        command=command,
        risk=risk,
    )


@pytest.fixture
def live():
    """Yield ``start(channel, **kw) -> (sess, pump)`` and clean up after."""
    pumps = []

    def start(channel, **kwargs):
        sess = make_session(channel, **kwargs)
        pump = Pump(sess)
        pump.start()
        pumps.append((pump, channel))
        return sess, pump

    yield start

    for pump, channel in pumps:
        pump.stop()
        channel.close()
