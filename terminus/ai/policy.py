"""Command safety policy — the hard boundary for AI-proposed execution.

Every command the model proposes is classified into a risk tier and checked
against structural rules before it can be presented for approval. Prompt
instructions are advisory; **this module is the control**. It fails closed:
anything unrecognised is refused.

Tiers:
    read_only    non-mutating, bounded  → one-click approval
    mutating     changes running state  → typed confirmation (phase 2)
    destructive  disruptive / removes   → typed confirmation + hostnames
    forbidden    never runnable, any phase

Pure functions, no I/O — trivially unit-testable.

File path: terminus/ai/policy.py
"""

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

RISK_READ_ONLY = "read_only"
RISK_MUTATING = "mutating"
RISK_DESTRUCTIVE = "destructive"
RISK_FORBIDDEN = "forbidden"
RISK_UNKNOWN = "unknown"

# Ordered for gating. `unknown` sits above read_only so it is never runnable
# under a read-only ceiling, but below forbidden so a future "allow unknown
# with confirmation" mode stays expressible.
_RISK_RANK = {
    RISK_READ_ONLY: 0,
    RISK_UNKNOWN: 1,
    RISK_MUTATING: 2,
    RISK_DESTRUCTIVE: 3,
    RISK_FORBIDDEN: 99,
}

# Plan-level caps.
MAX_COMMANDS_PER_PLAN = 10
MAX_COMMANDS_PER_SESSION = 6
MAX_COMMAND_CHARS = 200

_PRINTABLE_RE = re.compile(r"^[\x20-\x7e]+$")


def rank(risk):
    return _RISK_RANK.get(risk, 99)


@dataclass
class Verdict:
    """Result of classifying a single command."""

    ok: bool  # False → structurally rejected, never runnable
    risk: str
    reason: str
    normalized: str


@dataclass
class PlanItem:
    alias: str
    session_id: str
    hostname: str
    device_type: str
    command: str
    risk: str = RISK_UNKNOWN
    reason: str = ""


@dataclass
class ValidatedPlan:
    approved: list = field(default_factory=list)  # [PlanItem]
    blocked: list = field(default_factory=list)  # [PlanItem] with reason
    max_risk: str = RISK_READ_ONLY  # highest tier in `approved`

    @property
    def needs_confirmation(self):
        return rank(self.max_risk) > rank(RISK_READ_ONLY)


# ---------------------------------------------------------------------------
# Device families
# ---------------------------------------------------------------------------
_FAMILY_BY_PREFIX = (
    ("cisco_xr", "cisco"),
    ("cisco_nxos", "cisco"),
    ("cisco_asa", "cisco"),
    ("cisco_wlc", "cisco"),
    ("cisco", "cisco"),
    ("arista", "arista"),
    ("juniper", "juniper"),
    ("hp_comware", "hp"),
    ("hp_procurve", "hp"),
    ("hp", "hp"),
    ("paloalto", "paloalto"),
    ("fortinet", "fortinet"),
    ("f5", "f5"),
    ("linux", "linux"),
    ("local-shell", "linux"),
)


def family_for(device_type):
    """Map a Netmiko device_type onto a coarse command family."""
    dt = (device_type or "").lower()
    for prefix, fam in _FAMILY_BY_PREFIX:
        if dt.startswith(prefix):
            return fam
    return "unknown"


_NETWORK_FAMILIES = {
    "cisco",
    "arista",
    "juniper",
    "hp",
    "paloalto",
    "fortinet",
    "f5",
}


# ---------------------------------------------------------------------------
# Tier tables
# ---------------------------------------------------------------------------
# Never runnable, in any phase, on any platform.
_FORBIDDEN = (
    r"^reload\b",
    r"^reboot\b",
    r"^halt\b",
    r"^poweroff\b",
    r"^erase\b",
    r"^write\s+erase\b",
    r"^format\b",
    r"^mkfs\b",
    r"^dd\b",
    r"^rm\s+-{1,2}[rf]",
    r"^rm\s+.*\s-{1,2}[rf]",
    r"^factory-reset\b",
    r"^boot\s+system\b",
    r"^squeeze\b",
    r"^request\s+system\s+(zeroize|reboot|halt|power-off|software)",
    r"^request\s+(shutdown|restart)\b",
    r"^system\s+reset\b",
    r"^execute\s+(reboot|shutdown|formatlogdisk)",
    r"^upgrade\b",
    r"^install\b",
    r"^init\s+\d",
    r"^delete\s+/force",
    r"^tmsh\s+(load|save)\s+sys\s+config\s+default",
    r"^shutdown\s+-",
    r"^:\s*>\s*/",
)

# Disruptive or removes state.
_DESTRUCTIVE = (
    r"^no\s+\S",
    r"^default\s+interface\b",
    r"^clear\s+ip\s+bgp\b",
    r"^clear\s+(arp|mac|line|log|logging)\b",
    r"^clear\s+ip\s+ospf\s+process\b",
    r"^copy\s+run\S*\s+start\S*",
    r"^copy\s+\S+\s+(flash|disk|nvram)",
    r"^reset\b",
    r"^undo\s+\S",
    r"^kill\b",
    r"^pkill\b",
    r"^killall\b",
)

# Changes running configuration or persists it.
_MUTATING = (
    r"^conf(ig(ure)?)?\b",
    r"^commit\b",
    r"^rollback\b",
    r"^write\b",
    r"^save\b",
    r"^load\s+(set|override|replace)\b",
    r"^clear\s+counters\b",
    r"^clear\s+statistics\b",
    r"^license\b",
    r"^crypto\b",
    r"^(interface|router|vlan|hostname|username|snmp-server|logging|ntp|aaa"
    r"|line|banner|service|spanning-tree|access-list|ip\s+route"
    r"|route-map|policy-map|class-map|vrf)\b",
    r"^systemctl\s+(start|stop|restart|reload|enable|disable|mask)\b",
    r"^(mv|cp|chmod|chown|ln|touch|mkdir|rmdir|useradd|usermod|passwd)\b",
    r"^(iptables|nft|ufw)\b",
)

# Read-only, shared across network families.
_READ_ONLY_NETWORK = (
    r"^show\b",
    r"^display\b",
    r"^dir\b",
    r"^get\b",
    r"^ping\b",
    r"^traceroute\b",
    r"^tracert\b",
    r"^terminal\s+(length|width)\b",
    r"^screen-length\b",
    r"^set\s+cli\s+(screen-length|pager)\b",  # Junos/PAN pager, exec-only
    r"^tmsh\s+(show|list)\b",
    r"^list\b",
    r"^diagnose\s+(hardware|sys\s+(status|top-summary))\b",
)

# Read-only, Linux/local shells.
_READ_ONLY_LINUX = (
    r"^ls\b",
    r"^ll\b",
    r"^cat\b",
    r"^head\b",
    r"^tail\b",
    r"^nl\b",
    r"^uname\b",
    r"^uptime\b",
    r"^hostnamectl\b",
    r"^pwd\b",
    r"^date\b",
    r"^df\b",
    r"^du\b",
    r"^free\b",
    r"^vmstat\b",
    r"^iostat\b",
    r"^mpstat\b",
    r"^ps\b",
    r"^who\b",
    r"^w\b",
    r"^id\b",
    r"^groups\b",
    r"^last\b",
    r"^netstat\b",
    r"^ss\b",
    r"^arp\b",
    r"^route\b",
    r"^dig\b",
    r"^nslookup\b",
    r"^host\b",
    r"^getent\b",
    r"^lsblk\b",
    r"^lscpu\b",
    r"^lsmod\b",
    r"^lspci\b",
    r"^lsusb\b",
    r"^dmesg\b",
    r"^journalctl\b",
    r"^sysctl\s+-a?\b",
    r"^stat\b",
    r"^wc\b",
    r"^env\b",
    r"^printenv\b",
    r"^which\b",
    r"^whereis\b",
    r"^mount$",
    r"^findmnt\b",
    r"^ip\b",
    r"^ifconfig\b",
    r"^ethtool\b",
    r"^systemctl\s+(status|show|list-units|list-unit-files|is-active"
    r"|is-enabled|cat)\b",
    r"^ping\b",
    r"^traceroute\b",
    r"^mtr\s+-r\b",
    r"^hostname$",
    r"^echo\b",
    r"^grep\b",
    r"^awk\b",
    r"^sort\b",
    r"^uniq\b",
    r"^ps\b",
    r"^top\b",
    r"^who\b",
    r"^w\b",
    r"^id\b",
    r"^groups\b",
    r"^last\b",
)

# Pipe targets permitted on network devices.
_NETWORK_PIPE_OK = re.compile(
    r"^\s*(include|exclude|section|begin|count|grep|match|find|no-more"
    r"|utility\s+(wc|grep))\b",
    re.I,
)

# Commands permitted as later stages of a Linux pipeline.
_LINUX_PIPE_OK = re.compile(
    r"^\s*(grep|egrep|fgrep|awk|sed|head|tail|wc|sort|uniq|cut|tr|column"
    r"|nl|cat|less|more|jq|xargs\s+echo)\b",
    re.I,
)

# Targeted secret extraction — refused even when the base verb is read-only.
_SECRET_DUMP = (
    r"\|\s*(include|i|inc|sec|section|grep|match)\s+.*\b"
    r"(key|secret|password|passwd|community|psk|credential)\b",
    r"^more\s+system:running-config",
    r"^show\s+run\S*\s+all\b",
    r"^show\s+(key\s+chain|crypto\s+key|snmp\s+community|snmp-server\s+community)",
    r"^show\s+configuration\s+.*\bsecret\b",
    r"^tmsh\s+list\s+.*\bsecret\b",
    r"^cat\s+.*(shadow|id_rsa|id_ed25519|\.pem|\.key|credentials)",
    r"^(cat|less|more|head|tail)\s+.*/\.ssh/",
)

_SHELL_CHAINING = re.compile(r"[;&`]|\$\(|\|\||>>|(?<![0-9])>(?!=)")


def _matches(patterns, text):
    return any(re.search(p, text, re.I) for p in patterns)


# ---------------------------------------------------------------------------
# Structural validation
# ---------------------------------------------------------------------------
def _structural_check(command, family):
    """Return a rejection reason, or ``None`` if the shape is acceptable."""
    if not command:
        return "Empty command."
    if len(command) > MAX_COMMAND_CHARS:
        return f"Command exceeds {MAX_COMMAND_CHARS} characters."
    if "\n" in command or "\r" in command:
        return "Embedded newline — only one command per entry is allowed."
    if "\x1b" in command or not _PRINTABLE_RE.match(command):
        return "Command contains non-printable characters."

    if _matches(_SECRET_DUMP, command):
        return "Command targets credential material."

    if family == "linux":
        head, _, rest = command.partition("|")
        if _SHELL_CHAINING.search(head):
            return "Shell chaining / redirection is not permitted."
        for stage in rest.split("|") if rest else []:
            if _SHELL_CHAINING.search(stage):
                return "Shell chaining / redirection is not permitted."
            if stage.strip() and not _LINUX_PIPE_OK.match(stage):
                return f"Pipe target not permitted: {stage.strip()[:40]!r}"
            if re.search(r"\bsed\b.*\s-i\b", stage):
                return "In-place sed is not read-only."
    elif family in _NETWORK_FAMILIES or family == "unknown":
        if "|" in command:
            for stage in command.split("|")[1:]:
                if not _NETWORK_PIPE_OK.match(stage):
                    return f"Pipe target not permitted: {stage.strip()[:40]!r}"
        if ";" in command:
            return "Multiple commands on one line are not permitted."
    return None


# ---------------------------------------------------------------------------
# Sub-command guards (verbs that are read-only only in some forms)
# ---------------------------------------------------------------------------
def _guard_ip(command):
    if re.search(r"\b(set|add|del|delete|flush|change|replace)\b", command):
        return "`ip` with a mutating subcommand."
    return None


def _guard_ping(command, family):
    """Unbounded pings never terminate, so the capture would always time out."""
    if family != "linux":
        return None  # network platforms default to a count
    match = re.search(r"-c\s*(\d+)", command)
    if not match:
        return "Linux ping requires an explicit `-c <count>`."
    if int(match.group(1)) > 20:
        return "ping count is capped at 20."
    return None


def _guard_traceroute(command, family):
    if family == "linux" and not re.search(r"-m\s*\d+", command):
        return  # traceroute self-terminates
    return


def _guard_top(command):
    if not re.search(r"\s-b\b", command):
        return "`top` requires `-b` (batch mode) to terminate."
    return None


_GUARDS = {
    "ip": lambda c, f: _guard_ip(c),
    "ping": _guard_ping,
    "traceroute": _guard_traceroute,
    "top": lambda c, f: _guard_top(c),
}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def classify(command, device_type=""):
    """Classify a single command. Fails closed on anything unrecognised."""
    raw = command or ""
    family = family_for(device_type)

    if "\n" in raw or "\r" in raw:
        return Verdict(
            False,
            RISK_UNKNOWN,
            "Embedded newline — only one command per entry is allowed.",
            raw.strip(),
        )

    norm = " ".join(raw.strip().split())

    problem = _structural_check(norm, family)
    if problem:
        return Verdict(False, RISK_UNKNOWN, problem, norm)

    # Order matters: most dangerous first, so a broad read-only prefix can
    # never shadow a destructive form.
    if _matches(_FORBIDDEN, norm):
        return Verdict(
            False,
            RISK_FORBIDDEN,
            "Refused: this command can take the device offline.",
            norm,
        )

    if family == "linux" and re.match(r"^shutdown\b", norm, re.I):
        return Verdict(
            False, RISK_FORBIDDEN, "Refused: would power off the host.", norm
        )

    if _matches(_DESTRUCTIVE, norm):
        return Verdict(
            True, RISK_DESTRUCTIVE, "Removes or resets running state.", norm
        )

    if family in _NETWORK_FAMILIES and re.match(r"^shut(down)?\b", norm, re.I):
        return Verdict(
            True, RISK_DESTRUCTIVE, "Would disable an interface.", norm
        )

    # Junos `delete` edits the candidate config; elsewhere it removes files.
    if re.match(r"^delete\b", norm, re.I):
        if family == "juniper":
            return Verdict(
                True, RISK_MUTATING, "Edits the candidate configuration.", norm
            )
        return Verdict(True, RISK_DESTRUCTIVE, "Deletes a file.", norm)

    read_only = _READ_ONLY_LINUX if family == "linux" else _READ_ONLY_NETWORK
    if _matches(read_only, norm):
        head = norm.split()[0].lower()
        guard = _GUARDS.get(head)
        if guard:
            problem = guard(norm, family)
            if problem:
                return Verdict(False, RISK_UNKNOWN, problem, norm)
        return Verdict(True, RISK_READ_ONLY, "Read-only.", norm)

    if _matches(_MUTATING, norm):
        return Verdict(
            True, RISK_MUTATING, "Changes device configuration or state.", norm
        )

    return Verdict(
        False, RISK_UNKNOWN, "Not on the permitted command list.", norm
    )


# ---------------------------------------------------------------------------
# Plan validation
# ---------------------------------------------------------------------------
def validate_plan(
    targets,
    sessions,
    max_risk=RISK_READ_ONLY,
    max_commands=MAX_COMMANDS_PER_PLAN,
    max_per_session=MAX_COMMANDS_PER_SESSION,
):
    """Validate a model-proposed plan against live sessions and a risk ceiling.

    *targets* is ``[{"alias": str, "commands": [str]}]``.
    *sessions* maps alias → ``{"session_id", "hostname", "device_type"}``.

    Returns a :class:`ValidatedPlan`. Nothing is executed here.
    """
    plan = ValidatedPlan()
    ceiling = rank(max_risk)
    total = 0
    highest = RISK_READ_ONLY

    for target in targets or []:
        alias = str((target or {}).get("alias") or "").strip()
        info = sessions.get(alias)
        commands = (target or {}).get("commands") or []

        if not info:
            plan.blocked.append(
                PlanItem(
                    alias=alias or "?",
                    session_id="",
                    hostname="",
                    device_type="",
                    command="; ".join(map(str, commands))[:120],
                    risk=RISK_UNKNOWN,
                    reason="No such session in the current selection.",
                )
            )
            continue

        seen = set()
        per_session = 0
        for raw in commands:
            item = PlanItem(
                alias=alias,
                session_id=info.get("session_id", ""),
                hostname=info.get("hostname", ""),
                device_type=info.get("device_type", ""),
                command=str(raw),
            )

            verdict = classify(item.command, item.device_type)
            item.command = verdict.normalized
            item.risk = verdict.risk
            item.reason = verdict.reason

            key = item.command.lower()
            if key in seen:
                item.reason = "Duplicate command in this plan."
                plan.blocked.append(item)
                continue
            seen.add(key)

            if not verdict.ok or rank(verdict.risk) > ceiling:
                if verdict.ok:
                    item.reason = (
                        f"{verdict.reason} Requires approval level "
                        f"'{verdict.risk}'; this build permits '{max_risk}'."
                    )
                plan.blocked.append(item)
                continue

            if per_session >= max_per_session:
                item.reason = (
                    f"Exceeds the per-session limit of {max_per_session}."
                )
                plan.blocked.append(item)
                continue
            if total >= max_commands:
                item.reason = f"Exceeds the plan limit of {max_commands}."
                plan.blocked.append(item)
                continue

            plan.approved.append(item)
            per_session += 1
            total += 1
            if rank(item.risk) > rank(highest):
                highest = item.risk

    plan.max_risk = highest
    return plan


# ---------------------------------------------------------------------------
# Phase-2 helpers (defined now, unused while the ceiling is read_only)
# ---------------------------------------------------------------------------
_CONFIG_ENTER = re.compile(r"^conf(ig(ure)?)?\b", re.I)
_CONFIG_EXIT = re.compile(r"^(end|exit|quit|return|top)\b", re.I)


def check_mode_balance(commands):
    """Return a warning if a batch enters config mode without leaving it."""
    depth = 0
    for command in commands:
        if _CONFIG_ENTER.match(command.strip()):
            depth += 1
        elif _CONFIG_EXIT.match(command.strip()):
            depth = max(0, depth - 1)
    if depth:
        return (
            "This batch enters configuration mode but never returns to "
            "exec mode. Append `end` or the session will be left in "
            "config mode."
        )
    return None


def confirmation_phrase(plan):
    """Build the phrase a user must type verbatim to approve a risky plan."""
    devices = len({item.alias for item in plan.approved})
    return (
        f"apply {len(plan.approved)} command"
        f"{'' if len(plan.approved) == 1 else 's'} to {devices} device"
        f"{'' if devices == 1 else 's'}"
    )
