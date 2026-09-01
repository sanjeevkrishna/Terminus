"""Policy classification and plan validation.

The tables below are the security contract. A change that moves a command to a
lower tier should have to delete a line here to do it.

File path: tests/test_policy.py
"""

import pytest
from terminus.ai import policy
from terminus.ai.policy import (
    RISK_DESTRUCTIVE,
    RISK_FORBIDDEN,
    RISK_MUTATING,
    RISK_READ_ONLY,
    RISK_UNKNOWN,
    classify,
    validate_plan,
)

# (command, device_type, expected_risk, expected_ok)
CASES = [
    # -- read-only, network ------------------------------------------------
    ("show version", "cisco_ios", RISK_READ_ONLY, True),
    ("show ip interface brief", "cisco_ios", RISK_READ_ONLY, True),
    ("show running-config", "cisco_ios", RISK_READ_ONLY, True),
    ("display interface brief", "hp_comware", RISK_READ_ONLY, True),
    ("show interfaces terse", "juniper_junos", RISK_READ_ONLY, True),
    ("terminal width 511", "cisco_ios", RISK_READ_ONLY, True),
    ("show interfaces | include Ethernet", "cisco_ios", RISK_READ_ONLY, True),
    ("show log | count", "arista_eos", RISK_READ_ONLY, True),
    ("ping 8.8.8.8", "cisco_ios", RISK_READ_ONLY, True),
    # -- read-only, linux ---------------------------------------------------
    ("ls -la /var/log", "linux", RISK_READ_ONLY, True),
    ("uname -a", "linux", RISK_READ_ONLY, True),
    ("df -h", "linux", RISK_READ_ONLY, True),
    ("ip addr show", "linux", RISK_READ_ONLY, True),
    ("systemctl status nginx", "linux", RISK_READ_ONLY, True),
    ("journalctl -n 100", "linux", RISK_READ_ONLY, True),
    ("ping -c 4 8.8.8.8", "linux", RISK_READ_ONLY, True),
    ("ls -la | grep conf", "linux", RISK_READ_ONLY, True),
    ("top -b -n1", "linux", RISK_READ_ONLY, True),
    # -- mutating -----------------------------------------------------------
    ("configure terminal", "cisco_ios", RISK_MUTATING, True),
    ("conf t", "cisco_ios", RISK_MUTATING, True),
    ("interface Gi0/1", "cisco_ios", RISK_MUTATING, True),
    ("commit", "juniper_junos", RISK_MUTATING, True),
    ("write memory", "cisco_ios", RISK_MUTATING, True),
    ("clear counters", "cisco_ios", RISK_MUTATING, True),
    ("delete interfaces ge-0/0/1", "juniper_junos", RISK_MUTATING, True),
    ("systemctl restart nginx", "linux", RISK_MUTATING, True),
    ("chmod 640 /etc/hosts", "linux", RISK_MUTATING, True),
    # -- destructive --------------------------------------------------------
    ("no shutdown", "cisco_ios", RISK_DESTRUCTIVE, True),
    ("no router bgp 65000", "cisco_ios", RISK_DESTRUCTIVE, True),
    ("shutdown", "cisco_ios", RISK_DESTRUCTIVE, True),
    ("clear ip bgp *", "cisco_ios", RISK_DESTRUCTIVE, True),
    ("clear arp", "cisco_ios", RISK_DESTRUCTIVE, True),
    (
        "copy running-config startup-config",
        "cisco_ios",
        RISK_DESTRUCTIVE,
        True,
    ),
    ("delete flash:old.bin", "cisco_ios", RISK_DESTRUCTIVE, True),
    ("undo interface", "hp_comware", RISK_DESTRUCTIVE, True),
    ("kill 1234", "linux", RISK_DESTRUCTIVE, True),
    # -- forbidden ----------------------------------------------------------
    ("reload", "cisco_ios", RISK_FORBIDDEN, False),
    ("reload in 5", "cisco_ios", RISK_FORBIDDEN, False),
    ("reboot", "arista_eos", RISK_FORBIDDEN, False),
    ("write erase", "cisco_ios", RISK_FORBIDDEN, False),
    ("erase startup-config", "cisco_ios", RISK_FORBIDDEN, False),
    ("format flash:", "cisco_ios", RISK_FORBIDDEN, False),
    ("request system zeroize", "juniper_junos", RISK_FORBIDDEN, False),
    ("request system reboot", "juniper_junos", RISK_FORBIDDEN, False),
    ("factory-reset all", "cisco_ios", RISK_FORBIDDEN, False),
    ("boot system flash:image.bin", "cisco_ios", RISK_FORBIDDEN, False),
    ("rm -rf /", "linux", RISK_FORBIDDEN, False),
    ("rm -f /etc/passwd", "linux", RISK_FORBIDDEN, False),
    ("dd if=/dev/zero of=/dev/sda", "linux", RISK_FORBIDDEN, False),
    ("mkfs.ext4 /dev/sdb1", "linux", RISK_FORBIDDEN, False),
    ("halt", "linux", RISK_FORBIDDEN, False),
    ("poweroff", "linux", RISK_FORBIDDEN, False),
    ("shutdown -h now", "linux", RISK_FORBIDDEN, False),
    # -- unknown → refused --------------------------------------------------
    ("frobnicate the widget", "cisco_ios", RISK_UNKNOWN, False),
    ("curl http://example.com", "linux", RISK_UNKNOWN, False),
    ("nc -l 4444", "linux", RISK_UNKNOWN, False),
    ("mount /dev/sda1 /mnt", "linux", RISK_UNKNOWN, False),
]


@pytest.mark.parametrize("command,device_type,risk,ok", CASES)
def test_classification(command, device_type, risk, ok):
    verdict = classify(command, device_type)
    assert verdict.risk == risk, f"{command!r} → {verdict.reason}"
    assert verdict.ok is ok, f"{command!r} → {verdict.reason}"


# --------------------------------------------------------------------------
# Structural rules — these are bypass attempts, all must be refused
# --------------------------------------------------------------------------
BYPASS = [
    ("show version\nreload", "cisco_ios", "newline"),
    ("show version\rreload", "cisco_ios", "newline"),
    ("show version; reload", "cisco_ios", "one line"),
    ("ls -la; rm -rf /", "linux", "chaining"),
    ("ls && reboot", "linux", "chaining"),
    ("ls || reboot", "linux", "chaining"),
    ("echo `reboot`", "linux", "chaining"),
    ("echo $(reboot)", "linux", "chaining"),
    ("cat /etc/hosts > /etc/passwd", "linux", "redirect"),
    ("cat /etc/hosts >> /etc/passwd", "linux", "redirect"),
    ("show run | include password", "cisco_ios", "credential"),
    ("show running-config | i key", "cisco_ios", "credential"),
    ("more system:running-config", "cisco_ios", "credential"),
    ("show snmp community", "cisco_ios", "credential"),
    ("cat /etc/shadow", "linux", "credential"),
    ("cat /home/u/.ssh/id_rsa", "linux", "credential"),
    ("cat /root/.ssh/authorized_keys", "linux", "credential"),
    ("show version | exec reload", "cisco_ios", "pipe target"),
    ("ls | xargs rm", "linux", "pipe target"),
    ("cat f | sed -i s/a/b/ g", "linux", "in-place sed"),
    ("show ver\x1b[2J", "cisco_ios", "non-printable"),
    ("ip route add 0.0.0.0/0 via 1.1.1.1", "linux", "mutating ip"),
    ("ip link set eth0 down", "linux", "mutating ip"),
    ("ping 8.8.8.8", "linux", "unbounded ping"),
    ("ping -c 9999 8.8.8.8", "linux", "ping count cap"),
    ("top", "linux", "top needs batch mode"),
    ("show " + "x" * 300, "cisco_ios", "length"),
    ("", "cisco_ios", "empty"),
]


@pytest.mark.parametrize("command,device_type,label", BYPASS)
def test_structural_rejection(command, device_type, label):
    verdict = classify(command, device_type)
    assert verdict.ok is False, f"{label}: {command!r} was permitted"


def test_ip_show_is_not_mistaken_for_ip_add():
    """`ip addr show` must not trip the `\badd\b` guard."""
    assert classify("ip addr show", "linux").ok is True
    assert classify("ip -br addr", "linux").ok is True


def test_local_shell_uses_linux_family():
    assert policy.family_for("local-shell") == "linux"
    assert classify("ls", "local-shell").risk == RISK_READ_ONLY


def test_unknown_family_falls_back_to_network_rules():
    assert classify("show version", "").risk == RISK_READ_ONLY
    assert classify("ls -la", "").ok is False


# --------------------------------------------------------------------------
# Plan validation
# --------------------------------------------------------------------------
SESSIONS = {
    "S1": {
        "session_id": "a",
        "hostname": "core-sw1",
        "device_type": "cisco_ios",
    },
    "S2": {
        "session_id": "b",
        "hostname": "core-sw2",
        "device_type": "cisco_ios",
    },
    "S3": {"session_id": "c", "hostname": "web-01", "device_type": "linux"},
}


def test_happy_path():
    plan = validate_plan(
        [
            {"alias": "S1", "commands": ["show version", "show ip int br"]},
            {"alias": "S3", "commands": ["uname -a"]},
        ],
        SESSIONS,
    )
    assert len(plan.approved) == 3
    assert plan.blocked == []
    assert plan.max_risk == RISK_READ_ONLY
    assert plan.needs_confirmation is False


def test_unknown_alias_is_blocked():
    plan = validate_plan(
        [{"alias": "S9", "commands": ["show version"]}], SESSIONS
    )
    assert plan.approved == []
    assert "No such session" in plan.blocked[0].reason


def test_read_only_ceiling_blocks_mutating():
    plan = validate_plan(
        [{"alias": "S1", "commands": ["show version", "configure terminal"]}],
        SESSIONS,
        max_risk=RISK_READ_ONLY,
    )
    assert [i.command for i in plan.approved] == ["show version"]
    assert plan.blocked[0].risk == RISK_MUTATING
    assert "Requires approval level" in plan.blocked[0].reason


def test_raised_ceiling_admits_mutating_but_not_forbidden():
    plan = validate_plan(
        [{"alias": "S1", "commands": ["configure terminal", "reload"]}],
        SESSIONS,
        max_risk=RISK_DESTRUCTIVE,
    )
    assert [i.command for i in plan.approved] == ["configure terminal"]
    assert plan.max_risk == RISK_MUTATING
    assert plan.needs_confirmation is True
    assert plan.blocked[0].risk == RISK_FORBIDDEN


def test_forbidden_is_blocked_at_every_ceiling():
    for ceiling in (
        RISK_READ_ONLY,
        RISK_MUTATING,
        RISK_DESTRUCTIVE,
        RISK_FORBIDDEN,
    ):
        plan = validate_plan(
            [{"alias": "S1", "commands": ["reload"]}],
            SESSIONS,
            max_risk=ceiling,
        )
        assert plan.approved == [], f"reload permitted at ceiling {ceiling}"


def test_duplicate_commands_blocked():
    plan = validate_plan(
        [{"alias": "S1", "commands": ["show version", "SHOW  VERSION"]}],
        SESSIONS,
    )
    assert len(plan.approved) == 1
    assert "Duplicate" in plan.blocked[0].reason


def test_per_session_limit():
    plan = validate_plan(
        [
            {
                "alias": "S1",
                "commands": [f"show interface Gi0/{n}" for n in range(9)],
            }
        ],
        SESSIONS,
        max_per_session=3,
    )
    assert len(plan.approved) == 3
    assert all("per-session limit" in i.reason for i in plan.blocked)


def test_plan_limit_across_sessions():
    plan = validate_plan(
        [
            {"alias": "S1", "commands": ["show version", "show clock"]},
            {"alias": "S2", "commands": ["show version", "show clock"]},
        ],
        SESSIONS,
        max_commands=3,
    )
    assert len(plan.approved) == 3
    assert "plan limit" in plan.blocked[0].reason


def test_device_type_is_taken_from_the_session_not_the_model():
    """A linux command aimed at a Cisco session must be refused."""
    plan = validate_plan([{"alias": "S1", "commands": ["ls -la"]}], SESSIONS)
    assert plan.approved == []


def test_confirmation_phrase():
    plan = validate_plan(
        [
            {
                "alias": "S1",
                "commands": ["configure terminal", "interface Gi0/1"],
            },
            {"alias": "S2", "commands": ["configure terminal"]},
        ],
        SESSIONS,
        max_risk=RISK_MUTATING,
    )
    assert policy.confirmation_phrase(plan) == "apply 3 commands to 2 devices"


def test_mode_balance():
    assert policy.check_mode_balance(["configure terminal", "interface Gi0/1"])
    assert (
        policy.check_mode_balance(
            ["configure terminal", "interface Gi0/1", "end"]
        )
        is None
    )
    assert policy.check_mode_balance(["show version"]) is None
