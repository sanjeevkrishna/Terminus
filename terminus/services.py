"""SSH connection service for Terminus.

Opens an interactive SSH session via Netmiko — directly, or through a jump
host (terminal-server style) — resolves the device platform, and prepares
the terminal (paging off, width, enable) before handing the raw channel to
the socket layer. This module is stateless: it holds no session state.

Platform resolution is tiered, cheapest first:
    1. an explicit device_type from the connector
    2. login-banner fingerprints (free — no commands sent)
    3. Unix prompt shape
    4. Netmiko's SSH_MAPPER_BASE probes (bounded reads)

File path: terminus/services.py
"""

import logging
import re
import socket
from time import sleep, time

from netmiko import ConnectHandler, redispatch
from netmiko.ssh_autodetect import SSH_MAPPER_BASE
from netmiko.terminal_server import TerminalServerSSH

from .logbanner import open_session_logfile

logger = logging.getLogger(__name__)

_BANNER_RULE = "=" * 60

# Prompt endings across vendors: Cisco/Arista/Juniper (# > $), csh (%),
# and bracketed styles such as F5 / Aruba ("] #").
_PROMPT_PATTERN = r"[\$#>%\]]\s*$"
_PASSWORD_PATTERN = r"assword"

# Unix/Linux shells advertise themselves in the prompt (user@host:path$).
_UNIX_PROMPT_RE = r"[\w.\-]+@[\w.\-]+:[^\n]*[$#%]\s*$"
_UNIX_DEVICE_TYPE = "linux"

# Escape sequences that can share a line with the prompt:
#   OSC  → ESC ] ... BEL|ST   (Ubuntu's OSC 3008 metadata, title sets)
#   CSI  → ESC [ ... final    (bracketed paste \x1b[?2004h, colours)
#   two-char escapes → ESC + single char
# \x5c is a literal backslash (the ST terminator) — written this way so the
# pattern survives copy/paste intact.
_ESCAPE_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\x5c)"
    r"|\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b[@-Z\x5c-_]"
)

# Detection probes must fail fast — a wrong platform simply errors and
# reprints the prompt, which ends the read.
_DETECT_READ_TIMEOUT = 6.0
_PREP_READ_TIMEOUT = 4.0

# Commands sent before probing so full output is returned.
_DISABLE_PAGING_CMDS = ("terminal length 0",)

# Responses meaning "this platform does not understand the probe command".
_INVALID_RESPONSES = (
    r"% Invalid input detected",
    r"syntax error, expecting",
    r"Error: Unrecognized command",
    r"%Error",
    r"command not found",
    r"Syntax Error: unexpected argument",
    r"% Unrecognized command found at",
)

# Login-banner fingerprints — checked before any probe command is issued.
_BANNER_FINGERPRINTS = (
    (r"JUNOS|Juniper", "juniper_junos"),
    (r"Arista", "arista_eos"),
    (r"NX-OS|Nexus", "cisco_nxos"),
    (r"IOS ?XR", "cisco_xr"),
    (r"Adaptive Security Appliance|ASA Version", "cisco_asa"),
    (r"Cisco IOS|IOS Software", "cisco_ios"),
    (r"PAN-OS|Palo Alto", "paloalto_panos"),
    (r"FortiGate|FortiOS", "fortinet"),
    (r"BIG-IP|TMOS", "f5_tmsh"),
    (r"Comware", "hp_comware"),
    (r"ProCurve", "hp_procurve"),
    (r"Ubuntu|Debian|CentOS|Red Hat|GNU/Linux|Alpine", "linux"),
)

# Detected keys that must be mapped onto a real Netmiko driver.
_TYPE_ALIASES = {
    "cisco_wlc_85": "cisco_wlc",
    "cisco_xr_2": "cisco_xr",
}

# Best-effort paging/width commands, used when the platform is unknown.
# Unsupported commands simply error out, which is harmless.
_FALLBACK_PREP_CMDS = (
    "terminal length 0",  # Cisco IOS / NX-OS
    "terminal width 511",  # Cisco IOS
    "terminal pager 0",  # Cisco ASA
    "set cli screen-length 0",  # Juniper
    "set cli pager off",  # Palo Alto
    "screen-length disable",  # HP Comware
    "no page",  # HP ProCurve
)

# States recognised while driving the jump-host login.
_LOGIN_STATES = {
    "error": r"denied|auth\w*\s*fail|refused|Could not resolve"
    r"|No route to host|Connection closed|Connection timed out"
    r"|no matching (?:key exchange|host key|cipher)",
    "hostkey": r"\(yes/no(?:/\[fingerprint\])?\)\s*\?",
    "password": _PASSWORD_PATTERN,
    "prompt": _PROMPT_PATTERN,
}


class TerminalSessionError(Exception):
    """Raised when an interactive session cannot be established."""


# ---------------------------------------------------------------------------
# Connector mapping
# ---------------------------------------------------------------------------
def connector_to_params(hostname, connector):
    """Map a stored connector dict + device hostname to connection params."""
    params = {
        "hostname": hostname,
        "username": connector.get("network_username", ""),
        "password": connector.get("network_password", ""),
        "device_type": connector.get("device_type") or "autodetect",
    }
    if connector.get("ssh_options"):
        params["ssh_options"] = connector["ssh_options"]
    if connector.get("jumphost_ip"):
        params["proxy"] = {
            "hostname": connector["jumphost_ip"],
            "username": connector.get("jumphost_username", ""),
            "password": connector.get("jumphost_password", ""),
        }
    return params


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------
def _clean_prompt(text):
    """Extract a bare prompt from raw channel output.

    Shell-integration escapes (Ubuntu's OSC 3008, bracketed-paste mode,
    title sets) share a line with the prompt and would otherwise be
    captured verbatim — they also leak into log filenames.
    """
    if not text:
        return ""
    stripped = _ESCAPE_RE.sub("", text)
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if line:
            return line
    return ""


# ---------------------------------------------------------------------------
# Device-type auto-detection
# ---------------------------------------------------------------------------
def _normalize_device_type(device_type):
    """Map detection-only platform keys onto real Netmiko drivers."""
    for alias, actual in _TYPE_ALIASES.items():
        if alias in device_type:
            return actual
    return device_type


def detect_device_type(session, paging_cmds=_FALLBACK_PREP_CMDS):
    """Probe the live channel and return the best-matching Netmiko platform.

    Uses the full paging sweep rather than the Cisco-only command: an
    unsupported command simply errors, but a *paged* probe response truncates
    and can match the wrong platform.
    """
    for cmd in paging_cmds:
        session.send_command(cmd)

    cache = {}
    for candidate, detect in SSH_MAPPER_BASE:
        cmd = detect["cmd"]
        if cmd not in cache:
            cache[cmd] = session.send_command(cmd) or ""
        response = cache[cmd]
        if not response:
            continue
        if any(
            re.search(p, response, re.IGNORECASE) for p in _INVALID_RESPONSES
        ):
            continue
        if any(
            re.search(p, response, re.IGNORECASE)
            for p in detect["search_patterns"]
        ):
            device_type = _normalize_device_type(candidate)
            logger.debug("Probe matched → %s", device_type)
            return device_type

    logger.warning("Device type could not be detected.")
    return ""


# ---------------------------------------------------------------------------
# Interactive session
# ---------------------------------------------------------------------------
class TerminalSession:
    """An interactive Netmiko SSH session, optionally via a jump host.

    Attributes:
        channel: the underlying Netmiko connection object.
        prompt: the full device prompt (e.g. ``switch#``).
        base_prompt: the prompt without its trailing character.
        device_type: the configured or resolved Netmiko platform.
        banner: login output accumulated while reading the first prompt.
    """

    def __init__(
        self,
        hostname,
        username,
        password,
        proxy=None,
        device_type="autodetect",
        ssh_options=None,
        **kwargs,
    ):
        self.hostname = hostname
        self.username = username
        self.password = password
        self.proxy = proxy
        self.device_type = device_type
        self.ssh_options = ssh_options
        self.kwargs = kwargs  # forwarded to Netmiko

        self.channel = None
        self.prompt = ""
        self.base_prompt = ""
        self.banner = ""

        if proxy:
            self._connect_via_proxy()
        else:
            self._connect_direct()

    # -- connection ----------------------------------------------------------
    def _connect_direct(self):
        """Connect straight to the device, then resolve and prepare it."""
        logger.debug("Connecting directly to %s", self.hostname)
        self.channel = ConnectHandler(
            ip=self.hostname,
            username=self.username,
            password=self.password,
            device_type="autodetect",  # no session prep; we redispatch later
            **self.kwargs,
        )
        self._update_prompt()
        self.device_type = self._resolve_device_type()
        self._prepare_channel()

    def _connect_via_proxy(self):
        """Connect to the jump host, SSH onward, then resolve and prepare."""
        logger.debug("Connecting via jump host %s", self.proxy["hostname"])
        self.channel = TerminalServerSSH(
            ip=self.proxy["hostname"],
            username=self.proxy.get("username", ""),
            password=self.proxy.get("password", ""),
            device_type="autodetect",
            **self.kwargs,
        )
        self._update_prompt()
        self._ssh_from_proxy()
        self.banner = ""  # discard the jump host's banner
        self._update_prompt()  # now reads the device's banner + prompt
        self.device_type = self._resolve_device_type()
        self._prepare_channel()

    # -- jump-host login -----------------------------------------------------
    def _clear_buffer(self):
        """Discard anything still sitting in the channel buffer."""
        try:
            self.channel.clear_buffer()
        except Exception:
            logger.debug("clear_buffer() unavailable.")

    def _read_for(self, patterns, timeout=30.0, interval=0.15):
        """Accumulate channel output until one of *patterns* matches.

        *patterns* maps a state name to a regex. Returns ``(state, buffer)``
        with ``state`` set to ``None`` on timeout. Deliberately more
        forgiving than ``read_until_pattern`` — the jump host echoes the ssh
        command next to its own prompt, which single-pattern reads mis-match.
        """
        buf = ""
        deadline = time() + timeout
        while time() < deadline:
            chunk = self.channel.read_channel()
            if chunk:
                buf += chunk
                for state, pattern in patterns.items():
                    if re.search(pattern, buf, re.IGNORECASE | re.MULTILINE):
                        return state, buf
            else:
                sleep(interval)
        return None, buf

    def _ssh_from_proxy(self):
        """Drive an `ssh` command on the jump host to reach the device."""
        cmd_parts = ["ssh"]
        if self.ssh_options:
            cmd_parts.extend(self.ssh_options.split())
        cmd_parts += [
            "-o",
            "StrictHostKeyChecking=no",
            "-l",
            self.username,
            self.hostname,
        ]
        cmd = " ".join(cmd_parts)

        logger.debug("Jump host SSH: %s", cmd)

        # Drop leftovers from _update_prompt() so the stale jump-host prompt
        # cannot be mistaken for post-login output.
        self._clear_buffer()
        self.channel.write_channel(self.channel.normalize_cmd(cmd))

        # Consume the echoed command line first, for the same reason.
        self._read_for({"echo": re.escape(self.hostname)}, timeout=10)

        password_sent = False
        for _ in range(6):  # bounded: host-key, password, prompt, slack
            state, buf = self._read_for(_LOGIN_STATES, timeout=30)

            if state == "error":
                raise TerminalSessionError(self._login_error(buf))

            if state == "hostkey":
                self.channel.write_channel(self.channel.normalize_cmd("yes"))
                continue

            if state == "password":
                if password_sent:
                    # Re-prompted → the password was rejected.
                    raise TerminalSessionError("Authentication failed")
                self.channel.write_channel(
                    self.channel.normalize_cmd(self.password)
                )
                password_sent = True
                continue

            if state == "prompt":
                logger.debug("Jump host login reached the device prompt.")
                return

            raise TerminalSessionError(
                "Timed out waiting for the device prompt via the jump host."
            )

        raise TerminalSessionError(
            "Jump host SSH did not reach a device prompt."
        )

    @staticmethod
    def _login_error(response):
        """Translate a failed-login response into a readable message."""
        if re.search(r"denied|auth\w*\s*fail", response, re.IGNORECASE):
            return "Authentication failed"
        if re.search(r"refused", response, re.IGNORECASE):
            return "Connection refused"
        if re.search(
            r"Could not resolve|No route to host", response, re.IGNORECASE
        ):
            return "Host unreachable"
        if re.search(r"no matching", response, re.IGNORECASE):
            return (
                "SSH algorithm mismatch — add SSH options on the "
                "connector (jump-host connections only)"
            )
        if re.search(
            r"Connection closed|Connection timed out", response, re.IGNORECASE
        ):
            return "Connection closed by the remote host"
        return "SSH login failed"

    # -- platform resolution -------------------------------------------------
    def _resolve_device_type(self):
        """Resolve the platform: explicit → banner → prompt shape → probes."""
        if self.device_type and self.device_type != "autodetect":
            logger.debug("Using configured device type: %s", self.device_type)
            return self.device_type

        for pattern, platform in _BANNER_FINGERPRINTS:
            if re.search(pattern, self.banner, re.IGNORECASE):
                logger.debug("Banner fingerprint → %s", platform)
                return platform

        if re.search(_UNIX_PROMPT_RE, self.prompt):
            logger.debug("Unix-style prompt → %s", _UNIX_DEVICE_TYPE)
            return _UNIX_DEVICE_TYPE

        return detect_device_type(self)

    # -- channel setup -------------------------------------------------------
    def _update_prompt(self):
        """Read the current prompt, accumulate the banner, mirror onto channel."""
        self.channel.write_channel(self.channel.RETURN)
        response = self.channel.read_until_pattern(_PROMPT_PATTERN)
        self.banner += response or ""
        self.prompt = _clean_prompt(response)
        self.base_prompt = (
            self.prompt[:-1] if len(self.prompt) > 1 else self.prompt
        )
        # Netmiko's send_command() expects base_prompt; keep them in sync so
        # probes read up to a real prompt instead of waiting out the timeout.
        self.channel.base_prompt = self.base_prompt
        logger.debug("Prompt: %s", self.prompt)

    def _redispatch(self, device_type):
        try:
            redispatch(self.channel, device_type, session_prep=False)
        except Exception as exc:
            logger.warning("Redispatch to %s failed: %s", device_type, exc)

    def _refresh_base_prompt(self):
        """Re-read base_prompt from the channel, sanitising escapes."""
        self.base_prompt = (
            _clean_prompt(getattr(self.channel, "base_prompt", ""))
            or self.base_prompt
        )

    def _fallback_prep(self):
        """Try common paging commands; unsupported ones simply error out."""
        for cmd in _FALLBACK_PREP_CMDS:
            self.send_command(cmd, read_timeout=_PREP_READ_TIMEOUT)

    def _prepare_channel(self):
        """Redispatch to the resolved driver, then normalise the terminal."""
        # A shell has no paging commands and no enable mode.
        if self.device_type == _UNIX_DEVICE_TYPE:
            self._redispatch(_UNIX_DEVICE_TYPE)
            self._refresh_base_prompt()
            return

        # Unknown platform: best-effort paging sweep.
        if not self.device_type:
            logger.debug("Unknown platform — best-effort terminal prep.")
            self._fallback_prep()
            self._refresh_base_prompt()
            return

        self._redispatch(self.device_type)

        # Enable secret defaults to the login password when not supplied.
        if not getattr(self.channel, "secret", ""):
            self.channel.secret = self.password

        for step in (
            "set_base_prompt",
            "set_terminal_width",
            "disable_paging",
        ):
            try:
                getattr(self.channel, step)()
            except Exception as exc:
                logger.debug("%s failed: %s", step, exc)

        try:
            self.channel.enable()
        except Exception as exc:
            logger.debug("enable() skipped: %s", exc)

        self._refresh_base_prompt()

    # -- interface used by detection + the socket layer ----------------------
    def send_command(self, cmd, read_timeout=_DETECT_READ_TIMEOUT):
        """Send a command and return its output (used during setup only).

        An explicit expect_string is critical: an unsupported command still
        reprints the prompt, so the read ends immediately instead of waiting
        out the timeout.
        """
        kwargs = {"read_timeout": read_timeout}
        if self.base_prompt:
            kwargs["expect_string"] = re.escape(self.base_prompt)
        try:
            return self.channel.send_command(cmd, **kwargs)
        except Exception as exc:
            logger.debug("send_command(%r) failed: %s", cmd, exc)
            return ""

    @property
    def remote_conn(self):
        """The raw Paramiko channel backing this session."""
        return self.channel.remote_conn

    def disconnect(self):
        """Close the SSH session."""
        try:
            self.channel.disconnect()
        except Exception:
            logger.debug("Disconnect failed.")

    # Alias so either name works at call sites.
    close = disconnect


# ---------------------------------------------------------------------------
# Channel tuning + logging
# ---------------------------------------------------------------------------
def _apply_tcp_nodelay(channel, tag=""):
    """Enable TCP_NODELAY on the channel's transport socket, if possible."""
    try:
        transport = channel.get_transport()
        sock = getattr(transport, "sock", None) if transport else None
        if sock:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return True
    except OSError as exc:
        logger.warning("[%s] TCP_NODELAY failed: %s", tag, exc)
    return False


def open_terminus(params, logfile=None, tag=""):
    """Open an interactive SSH session.

    Returns ``(conn, channel, base_prompt, tee)`` where ``channel`` is the
    raw Paramiko channel and ``tee`` is an open binary log handle (or None).
    """
    logger.debug("Terminus connect -> %s", params["hostname"])
    conn = TerminalSession(
        hostname=params["hostname"],
        username=params["username"],
        password=params["password"],
        proxy=params.get("proxy"),
        device_type=params.get("device_type") or "autodetect",
        ssh_options=params.get("ssh_options"),
    )

    channel = conn.remote_conn
    channel.settimeout(0.0)
    _apply_tcp_nodelay(channel, tag)

    base_prompt = (conn.base_prompt or "session").strip()
    tee = (
        open_session_logfile(
            logfile,
            "Terminus Log",
            {"Host": params["hostname"], "Prompt": base_prompt},
            tag=tag,
        )
        if logfile
        else None
    )
    return conn, channel, base_prompt, tee


def test_connection(params, tag="test"):
    """Attempt a connection then immediately close it.

    Returns ``(ok: bool, message: str)`` for the Connectors panel's
    'Test connection' button.
    """
    conn = None
    try:
        conn, _channel, base_prompt, _tee = open_terminus(params, tag=tag)
        platform = conn.device_type or "unknown platform"
        return True, f"Connected — prompt '{base_prompt}' ({platform})."
    except Exception as exc:  # Netmiko/Paramiko raise a variety of types
        logger.info("[%s] test connection failed: %s", tag, exc)
        return False, str(exc)
    finally:
        if conn is not None:
            conn.disconnect()
