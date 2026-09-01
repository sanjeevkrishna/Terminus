"""Redaction contract.

The disclaimer promises that credentials are masked before anything leaves the
machine. These tables are that promise, expressed as assertions. A rule that
stops working should break a test here rather than leak a key.

File path: tests/test_redaction.py
"""

import pytest
from terminus.ai.providers import redact, redact_messages

MASK = "«redacted»"

# (label, input, secret that must NOT survive)
LEAKS = [
    # -- Cisco --------------------------------------------------------------
    ("enable secret", "enable secret 5 $1$abc$xyzXYZ123", "xyzXYZ123"),
    ("enable password", "enable password 7 070C285F4D06", "070C285F4D06"),
    (
        "username secret",
        "username admin privilege 15 secret 0 Sup3rS3cret",
        "Sup3rS3cret",
    ),
    (
        "username password",
        "username ops password 7 104D000A0618",
        "104D000A0618",
    ),
    ("snmp community", "snmp-server community PRIVATE_STR RW", "PRIVATE_STR"),
    (
        "snmp host",
        "snmp-server host 10.0.0.1 version 2c TRAPSTRING",
        "TRAPSTRING",
    ),
    (
        "isakmp key",
        "crypto isakmp key MyPreSharedKey address 1.2.3.4",
        "MyPreSharedKey",
    ),
    ("tacacs key", "tacacs-server key 7 0822455D0A16", "0822455D0A16"),
    ("radius key", "radius-server key MyRadiusSecret", "MyRadiusSecret"),
    (
        "ntp auth key",
        "ntp authentication-key 1 md5 NtpSecret99",
        "NtpSecret99",
    ),
    ("key-string", "key-string 7 13061E010803", "13061E010803"),
    ("pre-shared-key", "pre-shared-key local MySharedPsk", "MySharedPsk"),
    ("wpa-psk", "wpa-psk ascii 0 WifiPassword123", "WifiPassword123"),
    (
        "type-9 hash",
        "username x secret 9 $9$abcd$EFGHijklMNOPqrst",
        "$9$abcd$EFGHijklMNOPqrst",
    ),
    # -- Juniper ------------------------------------------------------------
    (
        "junos snmp community",
        "set snmp community JunosPublic authorization read-only",
        "JunosPublic",
    ),
    (
        "junos plain-text",
        "set system root-authentication plain-text-password RootPass123",
        "RootPass123",
    ),
    (
        "junos encrypted",
        "encrypted-password $6$abcdefgh$IJKLMNOP",
        "$6$abcdefgh$IJKLMNOP",
    ),
    # -- other vendors ------------------------------------------------------
    ("comware key", "authentication-key cipher $c$3$AbCdEfGhIj", "AbCdEfGhIj"),
    ("f5 secret", 'secret "MyF5Secret123"', "MyF5Secret123"),
    ("community-string", "community-string PublicRO", "PublicRO"),
    # -- Linux / API --------------------------------------------------------
    (
        "shadow line",
        "root:$6$saltsalt$hashhashhashhash:19000:0:99999:7:::",
        "$6$saltsalt$hashhashhashhash",
    ),
    (
        "env var",
        "export API_KEY=sk_live_9f8e7d6c5b4a3210",
        "sk_live_9f8e7d6c5b4a3210",
    ),
    (
        "json token",
        '{"access_token": "abc123def456ghi789"}',
        "abc123def456ghi789",
    ),
    ("json password", '{"password":"hunter2hunter2"}', "hunter2hunter2"),
    (
        "auth header",
        "Authorization: Bearer abcdefghijklmnop",
        "abcdefghijklmnop",
    ),
    (
        "basic auth",
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "dXNlcjpwYXNzd29yZA==",
    ),
]


@pytest.mark.parametrize(
    "label,text,secret", LEAKS, ids=[case[0] for case in LEAKS]
)
def test_secret_does_not_survive(label, text, secret):
    assert secret not in redact(text), f"{label}: secret leaked"


# --------------------------------------------------------------------------
# Key material
# --------------------------------------------------------------------------
def test_complete_pem_block():
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEAxyz123secretkeymaterialhere\n"
        "abcdefghijklmnopqrstuvwxyz0123456789\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = redact(text)
    assert "MIIEpAIBAAKCAQEAxyz123secretkeymaterialhere" not in out
    assert MASK in out


def test_unterminated_pem_block():
    """A tail-truncated log very often cuts a key mid-block."""
    text = (
        "some output\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n"
        "AAAAMwAAAAtzc2gtZWQyNTUxOQAAACBsecretkeycontinues"
    )
    out = redact(text)
    assert "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ" not in out
    assert "secretkeycontinues" not in out


def test_ssh_public_key_blob():
    text = (
        "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQDlongkeymaterialxyz user@host"
    )
    out = redact(text)
    assert "AAAAB3NzaC1yc2EAAAADAQABAAABgQDlongkeymaterialxyz" not in out


def test_jwt():
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    )
    assert jwt not in redact(f"token={jwt}")


def test_urlsafe_base64_blob():
    """The old catch-all missed `-` and `_`, so JWTs and API tokens survived."""
    blob = "abcdefgh-ijklmnop_qrstuvwx-yzABCDEF_GHIJKLMN-OPQRSTUV"
    assert blob not in redact(f"opaque value {blob} follows")


def test_long_hex_blob():
    blob = "a3f8c2d19e4b7f60a3f8c2d19e4b7f60a3f8c2d1"
    assert blob not in redact(f"digest {blob}")


def test_certificate_fingerprint():
    fingerprint = ":".join(["ab"] * 20)
    assert fingerprint not in redact(f"SHA1 Fingerprint={fingerprint}")


# --------------------------------------------------------------------------
# Over-masking guards — diagnostics must stay readable
# --------------------------------------------------------------------------
KEEP = [
    ("kex error", "no matching key exchange method found", "key exchange"),
    ("key chain", "show key chain", "key chain"),
    ("key length", "key length 2048", "key length"),
    (
        "interface",
        "GigabitEthernet0/1 is up, line protocol is up",
        "GigabitEthernet0/1",
    ),
    ("ip address", "ip address 10.20.30.40 255.255.255.0", "10.20.30.40"),
    ("mac address", "0050.56ab.cdef", "0050.56ab.cdef"),
    ("bgp state", "10.0.0.2 4 65001 1234 5678 Established", "Established"),
    (
        "counters",
        "5 minute input rate 1000 bits/sec, 2 packets/sec",
        "1000 bits/sec",
    ),
    ("hostname", "hostname core-sw1-lab", "core-sw1-lab"),
    ("version", "Cisco IOS Software, Version 15.2(4)E10", "15.2(4)E10"),
    ("uptime", "uptime is 3 weeks, 2 days, 5 hours", "3 weeks"),
    ("short mac colon", "aa:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:ff"),
]


@pytest.mark.parametrize(
    "label,text,keep", KEEP, ids=[case[0] for case in KEEP]
)
def test_useful_output_survives(label, text, keep):
    assert keep in redact(text), f"{label}: over-masked"


# --------------------------------------------------------------------------
# Properties
# --------------------------------------------------------------------------
def test_directive_is_preserved_so_context_survives():
    out = redact("enable secret 5 $1$abc$xyz")
    assert "enable secret" in out
    assert MASK in out


def test_idempotent():
    once = redact("username admin secret Sup3rS3cret")
    assert redact(once) == once


def test_empty_and_none():
    assert redact("") == ""
    assert redact(None) is None


def test_multiline_config_block():
    config = "\n".join(
        [
            "hostname core-sw1",
            "enable secret 5 $1$xyz$AbCdEfGhIj",
            "username ops privilege 15 secret 0 PlainTextPass",
            "snmp-server community SecretRO RO",
            "interface Vlan10",
            " ip address 10.1.1.1 255.255.255.0",
            "ntp authentication-key 1 md5 NtpKey99",
        ]
    )
    out = redact(config)
    for secret in (
        "$1$xyz$AbCdEfGhIj",
        "PlainTextPass",
        "SecretRO",
        "NtpKey99",
    ):
        assert secret not in out
    for keep in ("hostname core-sw1", "interface Vlan10", "10.1.1.1"):
        assert keep in out


def test_redact_messages_covers_every_role():
    messages = [
        {"role": "system", "content": "You are an engineer."},
        {"role": "user", "content": "check snmp-server community LEAKME"},
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "enable secret 5 $1$abc$LEAKTOO",
        },
    ]
    out = redact_messages(messages)
    assert "LEAKME" not in out[1]["content"]
    assert "LEAKTOO" not in out[2]["content"]
    assert out[2]["tool_call_id"] == "c1"  # non-content keys preserved


def test_redact_messages_leaves_tool_calls_intact():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "run_commands", "arguments": "{}"},
                }
            ],
        }
    ]
    out = redact_messages(messages)
    assert out[0]["tool_calls"][0]["id"] == "c1"
