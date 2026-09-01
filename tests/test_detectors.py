"""Completion detection and output cleanup.

File path: tests/test_detectors.py
"""

import pytest
from terminus.ai.executor import (
    PromptDetector,
    SentinelDetector,
    _finalize_output,
    _strip_echo,
    _strip_trailing_prompt,
    detector_for,
    looks_like_error,
    timeout_for,
)


# --------------------------------------------------------------------------
# PromptDetector
# --------------------------------------------------------------------------
def test_completes_on_prompt_after_quiet():
    det = PromptDetector("switch", quiet=0.1)
    text = "show version\nCisco IOS Software\nswitch#"
    assert det.is_complete(text, idle_for=0.2) is True


def test_incomplete_while_output_still_flowing():
    det = PromptDetector("switch", quiet=0.1)
    text = "show version\nCisco IOS Software\nswitch#"
    assert det.is_complete(text, idle_for=0.01) is False


def test_prompt_inside_output_does_not_complete():
    """The single most important case: a prompt quoted mid-output."""
    det = PromptDetector("switch", quiet=0.1)
    text = (
        "show archive config\n"
        "  line 1: switch# configure terminal\n"
        "  line 2: switch(config)# interface Gi0/1\n"
        "still streaming more archive content"
    )
    assert det.is_complete(text, idle_for=5.0) is False


def test_config_mode_prompts_complete():
    det = PromptDetector("switch", quiet=0.1)
    for prompt in (
        "switch(config)#",
        "switch(config-if)#",
        "switch(config-router)#",
        "switch(config-line)#",
    ):
        assert det.is_complete(f"output\n{prompt}", 0.2) is True, prompt


@pytest.mark.parametrize(
    "prompt",
    [
        "rtr-01>",
        "fw1$",
        "leaf-3%",
        "bigip1]",
        "vsrx-1#",
    ],
)
def test_various_prompt_endings(prompt):
    base = prompt[:-1]
    det = PromptDetector(base, quiet=0.1)
    assert det.is_complete(f"some output\n{prompt}", 0.2) is True


def test_no_base_prompt_falls_back_to_generic_with_longer_silence():
    det = PromptDetector("", quiet=0.1)
    assert det.quiet >= 0.6
    assert det.is_complete("output\nunknown-host#", 0.3) is False
    assert det.is_complete("output\nunknown-host#", 0.7) is True


def test_only_the_tail_is_examined():
    det = PromptDetector("switch", quiet=0.1)
    text = "switch#\n" + ("filler line\n" * 200)
    assert det.is_complete(text, 1.0) is False


# --------------------------------------------------------------------------
# SentinelDetector
# --------------------------------------------------------------------------
def test_sentinel_needs_two_occurrences():
    det = SentinelDetector("__TERMINUS_abc__")
    assert det.is_complete("ls ; echo __TERMINUS_abc__\n", 5.0) is False
    assert (
        det.is_complete(
            "ls ; echo __TERMINUS_abc__\nfile\n__TERMINUS_abc__\n", 0.0
        )
        is True
    )


def test_sentinel_ignores_idle_time():
    det = SentinelDetector("__T__")
    assert det.is_complete("__T__ x __T__", idle_for=0.0) is True


def test_sentinel_suffix_shape():
    det = SentinelDetector("__T__")
    assert det.suffix() == " ; echo __T__"


@pytest.mark.parametrize(
    "device_type,expected",
    [
        ("linux", SentinelDetector),
        ("local-shell", SentinelDetector),
        ("cisco_ios", PromptDetector),
        ("juniper_junos", PromptDetector),
        ("", PromptDetector),
    ],
)
def test_detector_selection(device_type, expected):
    assert isinstance(detector_for(device_type, "host"), expected)


# --------------------------------------------------------------------------
# Output cleanup
# --------------------------------------------------------------------------
def test_strip_echo():
    assert (
        _strip_echo("show version\nCisco IOS\n", "show version") == "Cisco IOS"
    )


def test_strip_echo_leaves_body_when_absent():
    assert _strip_echo("Cisco IOS\n", "show version") == "Cisco IOS\n"


def test_strip_trailing_prompt():
    det = PromptDetector("switch", quiet=0.1)
    assert _strip_trailing_prompt("Cisco IOS\nswitch#", det) == "Cisco IOS"


def test_strip_trailing_prompt_removes_sentinel_lines():
    det = SentinelDetector("__T__")
    text = "ls ; echo __T__\nfile.txt\n__T__\nuser@host:~$"
    assert _strip_trailing_prompt(text, det) == "file.txt"


def test_finalize_tail_truncates_on_a_line_boundary():
    det = PromptDetector("switch", quiet=0.1)
    body = "\n".join(f"line {n}" for n in range(5000))
    text, truncated = _finalize_output(
        f"show log\n{body}\nswitch#", "show log", det, budget=2000
    )
    assert truncated is True
    assert len(text) <= 2000
    assert not text.startswith("ine")  # no partial first line


def test_finalize_no_truncation_when_within_budget():
    det = PromptDetector("switch", quiet=0.1)
    text, truncated = _finalize_output(
        "show clock\n09:00:00 UTC\nswitch#", "show clock", det, budget=10000
    )
    assert truncated is False
    assert text == "09:00:00 UTC"


@pytest.mark.parametrize(
    "text",
    [
        "% Invalid input detected at '^' marker.",
        "syntax error, expecting <command>",
        "Error: Unrecognized command",
        "%Error opening file",
        "bash: frobnicate: command not found",
        "% Unrecognized command found at '^' position.",
    ],
)
def test_error_detection(text):
    assert looks_like_error(text) is True


def test_normal_output_is_not_an_error():
    assert looks_like_error("Cisco IOS Software, Version 15.2") is False


def test_slow_commands_get_the_long_timeout():
    from terminus.ai.executor import DEFAULT_TIMEOUT, SLOW_TIMEOUT

    assert timeout_for("show tech-support") == SLOW_TIMEOUT
    assert timeout_for("traceroute 8.8.8.8") == SLOW_TIMEOUT
    assert timeout_for("journalctl -n 5000") == SLOW_TIMEOUT
    assert timeout_for("show version") == DEFAULT_TIMEOUT
