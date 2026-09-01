"""Command execution against live interactive sessions.

The socket read loop is the *only* reader of any channel. This module never
calls ``channel.recv()``. Instead it registers a :class:`Capture` on the
session; :func:`terminus.sockets.read_output` fans incoming bytes into it
alongside the client emit and the session log. The user therefore watches
every AI-issued command run in their own terminal, in real time.

Completion detection is layered, because no single method works across
vendors:

    1. prompt match anchored at the *end* of the buffer
    2. followed by a quiet period (guards against a prompt inside output)
    3. hard per-command timeout, returning partial output
    4. paging guard (paging should already be off; treat one as a defect)
    5. sentinel echo on POSIX, which is far more reliable than prompt matching

Ordering guarantee: the log/transcript marker is queued *before* the bytes are
written to the channel, so a marker can never land after the command echo it
labels.

File path: terminus/ai/executor.py
"""

import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from ..services import _INVALID_RESPONSES, _PROMPT_PATTERN
from ..transcript import SessionMonitor, clean_text  # noqa: F401

logger = logging.getLogger(__name__)

_MARK = "[Terminus AI]"

# -- timing -----------------------------------------------------------------
DEFAULT_TIMEOUT = 30.0
SLOW_TIMEOUT = 120.0
_QUIET_PERIOD = 0.20  # silence required after a prompt match
_POLL_INTERVAL = 0.05
_PREFLIGHT_IDLE = 0.30  # channel must be this quiet before we send
_PREFLIGHT_WAIT = 2.0  # how long to wait for it to settle
_LOCK_TIMEOUT = 5.0
_SETTLE_AFTER_SEND = 0.05
_ABORT_SETTLE = 0.35  # after Ctrl-C, let the device reprint its prompt
_INTER_COMMAND_GAP = 0.10  # breathing room between commands on one session

# -- limits -----------------------------------------------------------------
MAX_OUTPUT_CHARS = 40 * 1024  # per command, fed to the model
_CAPTURE_HARD_CAP = 4 * 1024 * 1024  # per command, in memory
MAX_TURN_OUTPUT_CHARS = 200 * 1024  # per approved plan, across all sessions
MAX_WORKERS = 8
BATCH_DEADLINE = 300.0

# Commands known to be slow enough to warrant the long timeout.
_SLOW_COMMANDS = re.compile(
    r"^(show\s+tech|show\s+running-config\s+all|show\s+logging"
    r"|show\s+archive|traceroute|tracert|ping|show\s+interfaces\s*$"
    r"|journalctl|dmesg|show\s+configuration\s*$|show\s+version\s+all)",
    re.I,
)

# Paging prompts. These should never appear — paging is disabled at connect —
# so treat one as a defect signal, answer it a bounded number of times, and
# flag the result so the model knows the output may be malformed.
_PAGING_RE = re.compile(
    rb"--\s*more\s*--|---\(more( \d+%)?\)---|<--- More --->"
    rb"|Press any key to continue|lines \d+-\d+",
    re.I,
)
_MAX_PAGE_ANSWERS = 3

# -- statuses ---------------------------------------------------------------
STATUS_OK = "ok"
STATUS_ERROR = "error"  # ran, but the device rejected it
STATUS_TIMEOUT = "timeout"
STATUS_BUSY = "busy"
STATUS_BLOCKED = "blocked"  # set by policy, never by this module
STATUS_WRONG_MODE = "wrong_mode"
STATUS_SESSION_GONE = "session_gone"
STATUS_CANCELLED = "cancelled"
STATUS_LOCKED = "locked"
STATUS_SKIPPED = "skipped"  # batch budget or deadline exhausted

_TERMINAL_STATUSES = frozenset(
    {
        STATUS_SESSION_GONE,
        STATUS_CANCELLED,
        STATUS_LOCKED,
        STATUS_WRONG_MODE,
    }
)


@dataclass
class CommandResult:
    """Outcome of one command on one session."""

    alias: str
    session_id: str
    hostname: str
    device_type: str
    command: str
    status: str
    output: str = ""
    truncated: bool = False
    paged: bool = False
    bytes_read: int = 0
    elapsed: float = 0.0
    risk: str = ""
    detail: str = ""

    def to_dict(self):
        """Shape handed to the model as a tool result."""
        return {
            "alias": self.alias,
            "hostname": self.hostname,
            "device_type": self.device_type,
            "command": self.command,
            "status": self.status,
            "output": self.output,
            "truncated": self.truncated,
            "paged": self.paged,
            "detail": self.detail,
        }

    def progress(self):
        """Compact shape for the live per-session progress rows."""
        return {
            "alias": self.alias,
            "session_id": self.session_id,
            "command": self.command,
            "status": self.status,
            "bytes": self.bytes_read,
            "elapsed": round(self.elapsed, 2),
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Completion detectors
# ---------------------------------------------------------------------------
def _prompt_regex(base_prompt):
    """Build an end-anchored prompt matcher, tolerating mode suffixes.

    ``switch`` must also match ``switch(config)#`` and ``switch(config-if)#``,
    because a command can legitimately change mode.
    """
    base = (base_prompt or "").strip()
    if not base:
        return None
    return re.compile(
        r"(?:^|\r|\n)[^\r\n]*"
        + re.escape(base)
        + r"(?:\([\w\-.:/]+\))*\s*[#>$%\]]\s*$"
    )


_GENERIC_PROMPT_RE = re.compile(_PROMPT_PATTERN)


class PromptDetector:
    """Complete when the buffer ends at a prompt and the channel goes quiet.

    Both conditions matter. A prompt string can appear *inside* output (a
    config excerpt, a log line quoting a prompt), so an end-of-buffer match
    alone is not sufficient — the quiet period is what makes it reliable.
    """

    name = "prompt"

    def __init__(self, base_prompt, quiet=_QUIET_PERIOD):
        self._re = _prompt_regex(base_prompt)
        # With no usable base_prompt we lean entirely on the generic ending,
        # which needs a longer silence to be trustworthy.
        self.quiet = quiet if self._re else max(quiet, 0.6)

    def is_complete(self, text, idle_for):
        if idle_for < self.quiet:
            return False
        tail = text[-400:]
        if self._re is not None and self._re.search(tail):
            return True
        return bool(idle_for >= 0.6 and _GENERIC_PROMPT_RE.search(tail))


class SentinelDetector:
    """Complete when an echoed sentinel appears — reliable, POSIX only.

    The token is seen twice: once in the echoed command line, once when the
    shell actually runs the ``echo``. Waiting for the second occurrence
    removes all guesswork about prompts and quiet periods.
    """

    name = "sentinel"
    quiet = 0.0

    def __init__(self, token):
        self.token = token
        self._re = re.compile(re.escape(token))

    def suffix(self):
        return f" ; echo {self.token}"

    def is_complete(self, text, idle_for):
        return len(self._re.findall(text)) >= 2


def detector_for(device_type, base_prompt):
    """Pick the most reliable detector available for this platform."""
    dt = (device_type or "").lower()
    if dt.startswith(("linux", "local-shell")):
        return SentinelDetector(f"__TERMINUS_{uuid.uuid4().hex[:12]}__")
    return PromptDetector(base_prompt)


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------
class Capture:
    """Accumulates channel bytes for one command until the detector fires.

    ``feed()`` is called from the socket read loop, so it must stay cheap and
    must never block. All real work happens in ``wait()`` on the caller's
    thread.
    """

    def __init__(self, detector, send=None):
        self.detector = detector
        self._send = send
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._last_rx = time.monotonic()
        self.overflowed = False
        self.paged = False
        self._page_answers = 0

    # -- producer (read loop) -----------------------------------------------
    def feed(self, data: bytes):
        with self._lock:
            if len(self._buf) < _CAPTURE_HARD_CAP:
                self._buf.extend(data)
            else:
                self.overflowed = True
            self._last_rx = time.monotonic()
            needs_page = self._page_answers < _MAX_PAGE_ANSWERS and bool(
                _PAGING_RE.search(bytes(self._buf[-256:]))
            )
            if needs_page:
                self._page_answers += 1
                self.paged = True
        self._wake.set()

        if needs_page and self._send:
            # Paging should have been disabled during connect-time prep, so
            # reaching here means that failed. Answer, but flag the result.
            logger.warning(
                "Paging prompt during capture — answering (%d/%d).",
                self._page_answers,
                _MAX_PAGE_ANSWERS,
            )
            try:
                self._send(" ")
            except Exception:
                logger.debug("Could not answer paging prompt.", exc_info=True)

    # -- consumer ------------------------------------------------------------
    def text(self):
        with self._lock:
            return clean_text(bytes(self._buf))

    def size(self):
        with self._lock:
            return len(self._buf)

    def wait(self, timeout, cancel_event=None):
        """Block until complete, cancelled, or timed out.

        Returns ``STATUS_OK`` / ``STATUS_CANCELLED`` / ``STATUS_TIMEOUT``.
        Partial output remains available via :meth:`text` in every case.
        """
        deadline = time.monotonic() + timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return STATUS_CANCELLED
            if time.monotonic() >= deadline:
                return STATUS_TIMEOUT

            # Woken by feed(); the timeout also drives the quiet-period check
            # when no further bytes arrive.
            self._wake.wait(_POLL_INTERVAL)
            self._wake.clear()

            with self._lock:
                idle_for = time.monotonic() - self._last_rx
                text = clean_text(bytes(self._buf))
            try:
                if self.detector.is_complete(text, idle_for):
                    return STATUS_OK
            except Exception:
                logger.debug(
                    "Detector raised; treating as incomplete.", exc_info=True
                )


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
@dataclass
class Preflight:
    ok: bool
    status: str = STATUS_OK
    detail: str = ""


def preflight(sess, wait=_PREFLIGHT_WAIT):
    """Confirm the session is idle and sitting at a usable prompt.

    Refusing here is what prevents Terminus from corrupting a session where
    the user is mid-command or watching a long-running sweep.
    """
    if not sess:
        return Preflight(False, STATUS_SESSION_GONE, "Session has closed.")

    channel = sess.get("channel")
    if channel is None or getattr(channel, "closed", False):
        return Preflight(False, STATUS_SESSION_GONE, "Channel is closed.")

    monitor = sess.get("monitor")
    if monitor is None:
        return Preflight(False, STATUS_BUSY, "Session is not being monitored.")

    deadline = time.monotonic() + wait
    while monitor.idle_for() < _PREFLIGHT_IDLE:
        if time.monotonic() >= deadline:
            return Preflight(
                False, STATUS_BUSY, "Session is still producing output."
            )
        time.sleep(_POLL_INTERVAL)

    last = monitor.last_line()
    if "(config" in last.lower():
        # Never auto-`end`: leaving config mode is a state change the user did
        # not ask for, and may discard an in-progress edit.
        return Preflight(
            False,
            STATUS_WRONG_MODE,
            f"Session is in configuration mode ({last!r}). "
            f"Return to exec mode first.",
        )
    if last and not _GENERIC_PROMPT_RE.search(last):
        return Preflight(
            False,
            STATUS_BUSY,
            f"Session is not at a prompt (last line: {last[:60]!r}).",
        )
    return Preflight(True)


# ---------------------------------------------------------------------------
# Output cleanup
# ---------------------------------------------------------------------------
def _strip_echo(text, command):
    """Remove the echoed command line from the head of captured output."""
    if not command:
        return text
    lines = text.splitlines()
    for index, line in enumerate(lines[:4]):
        if command in line:
            return "\n".join(lines[index + 1 :])
    return text


def _strip_trailing_prompt(text, detector):
    """Remove the prompt — and any sentinel — the detector matched on."""
    lines = text.splitlines()
    if isinstance(detector, SentinelDetector):
        lines = [ln for ln in lines if detector.token not in ln]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and _GENERIC_PROMPT_RE.search(lines[-1].strip()):
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def looks_like_error(text):
    """True when the device rejected the command (unknown verb, bad syntax)."""
    return any(re.search(p, text, re.I) for p in _INVALID_RESPONSES)


def _finalize_output(raw, command, detector, budget=MAX_OUTPUT_CHARS):
    """Clean, trim and tail-truncate captured output. Returns (text, cut)."""
    text = _strip_trailing_prompt(_strip_echo(raw, command), detector)
    text = text.strip("\n")
    truncated = False
    if budget and len(text) > budget:
        text = text[-budget:]
        newline = text.find("\n")
        if 0 <= newline < 200:  # drop a partial first line
            text = text[newline + 1 :]
        truncated = True
    return text, truncated


def timeout_for(command):
    """Per-command timeout: long for known-slow commands, short otherwise."""
    return (
        SLOW_TIMEOUT
        if _SLOW_COMMANDS.match((command or "").strip())
        else DEFAULT_TIMEOUT
    )


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------
def _log_header(item):
    return (
        f"────── {_MARK} {item.risk} · {time.strftime('%H:%M:%S')} "
        f"──────\n$ {item.command}"
    )


def _log_footer(result):
    glyph = {"ok": "✔", "error": "✖", "timeout": "⏱"}.get(result.status, "•")
    return (
        f"────── {_MARK} {glyph} {result.status} · "
        f"{result.bytes_read:,} B · {result.elapsed:.1f}s ──────"
    )


def _term_header(item):
    return f"\r\n\x1b[2m── AI ▶ {item.command} ──\x1b[0m\r\n"


def _term_footer(result):
    glyph = {"ok": "✔", "error": "✖", "timeout": "⏱"}.get(result.status, "•")
    return (
        f"\r\n\x1b[2m── AI {glyph} {result.status} · "
        f"{result.bytes_read:,} B · {result.elapsed:.1f}s ──\x1b[0m\r\n"
    )


def _abort_running_command(channel):
    """Send Ctrl-C so an abandoned command cannot bleed into the next capture.

    Without this, a timed-out or cancelled command keeps writing to the
    channel and its output would be attributed to whatever runs next.
    """
    try:
        channel.send("\x03")
        time.sleep(_ABORT_SETTLE)
    except Exception:
        logger.debug("Could not send abort to channel.", exc_info=True)


# ---------------------------------------------------------------------------
# Single command
# ---------------------------------------------------------------------------
def run_one(
    sess,
    item,
    notify=None,
    cancel_event=None,
    timeout=None,
    output_budget=MAX_OUTPUT_CHARS,
):
    """Run one command on one session and return a :class:`CommandResult`.

    *item* is a :class:`terminus.policy.PlanItem` — already classified and
    approved. *notify* is ``notify(session_id, text)``, used to echo AI
    markers into the user's own terminal.
    """
    result = CommandResult(
        alias=item.alias,
        session_id=item.session_id,
        hostname=item.hostname,
        device_type=item.device_type,
        command=item.command,
        risk=item.risk,
        status=STATUS_OK,
    )

    if cancel_event is not None and cancel_event.is_set():
        result.status = STATUS_CANCELLED
        result.detail = "Cancelled before execution."
        return result

    if not sess:
        result.status = STATUS_SESSION_GONE
        result.detail = "Session has closed."
        return result

    lock = sess.get("exec_lock")
    if lock is None:
        result.status = STATUS_SESSION_GONE
        result.detail = "Session is not executable."
        return result
    if not lock.acquire(timeout=_LOCK_TIMEOUT):
        result.status = STATUS_LOCKED
        result.detail = "Another command is already running on this session."
        return result

    started = time.monotonic()
    session_log = sess.get("session_log")
    detector = detector_for(item.device_type, sess.get("base_prompt", ""))
    capture = None
    try:
        check = preflight(sess)
        if not check.ok:
            result.status = check.status
            result.detail = check.detail
            return result

        channel = sess["channel"]

        # Marker is queued first, synchronously, so it can never be written
        # after the command echo it labels.
        if session_log:
            session_log.write_marker(_log_header(item))
        if notify:
            notify(item.session_id, _term_header(item))

        capture = Capture(detector, send=channel.send)
        sess["capture"] = capture

        payload = item.command
        if isinstance(detector, SentinelDetector):
            payload += detector.suffix()

        try:
            channel.send(payload + "\r")
        except Exception as exc:
            result.status = STATUS_SESSION_GONE
            result.detail = f"Send failed: {exc}"
            return result

        time.sleep(_SETTLE_AFTER_SEND)

        wait_status = capture.wait(
            timeout if timeout is not None else timeout_for(item.command),
            cancel_event,
        )

        raw = capture.text()
        result.bytes_read = capture.size()
        result.paged = capture.paged
        result.output, cut = _finalize_output(
            raw, item.command, detector, output_budget
        )
        result.truncated = cut or capture.overflowed

        if wait_status == STATUS_OK:
            if looks_like_error(result.output):
                result.status = STATUS_ERROR
                result.detail = "The device rejected the command."
            else:
                result.status = STATUS_OK
        else:
            result.status = wait_status
            result.detail = (
                "Timed out waiting for the prompt; output may be incomplete."
                if wait_status == STATUS_TIMEOUT
                else "Cancelled while running; output may be incomplete."
            )
            _abort_running_command(channel)

        if capture.paged and result.detail:
            result.detail += " Paging was active — output may be malformed."
        elif capture.paged:
            result.detail = "Paging was active — output may be malformed."

        return result
    except Exception as exc:  # never leak into the pool
        logger.exception(
            "run_one failed for %s on %s.", item.command, item.alias
        )
        result.status = STATUS_ERROR
        result.detail = f"Execution error: {exc}"
        return result
    finally:
        result.elapsed = time.monotonic() - started
        sess["capture"] = None  # stop the read-loop fan-out
        if session_log and capture is not None:
            session_log.write_marker(_log_footer(result))
        if notify and capture is not None:
            notify(item.session_id, _term_footer(result))
        lock.release()


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------
def _group_by_session(items):
    """Preserve order within a session; sessions themselves run in parallel."""
    grouped = {}
    for item in items:
        grouped.setdefault(item.session_id, []).append(item)
    return grouped


def run_batch(
    items,
    get_session,
    notify=None,
    cancel_event=None,
    progress=None,
    deadline=BATCH_DEADLINE,
    turn_budget=MAX_TURN_OUTPUT_CHARS,
    max_workers=MAX_WORKERS,
):
    """Execute an approved plan.

    Commands on the *same* session run sequentially — they share one channel
    and one prompt, so overlapping them would interleave output. Different
    sessions run concurrently, because a 10-device report is unusable
    otherwise.

    *get_session* is ``get_session(session_id) -> dict | None``, resolved at
    execution time so a session closed mid-batch is detected rather than
    cached. *progress* is ``progress(dict)``, called with
    :meth:`CommandResult.progress` payloads as work completes.

    Returns ``[CommandResult]`` in the original plan order.
    """
    grouped = _group_by_session(items)
    if not grouped:
        return []

    results = {}
    budget = {"left": turn_budget}
    budget_lock = threading.Lock()
    stop_at = time.monotonic() + deadline

    def emit(result):
        results[id(result.item_ref)] = result
        if progress:
            try:
                progress(result.progress())
            except Exception:
                logger.debug("Progress callback failed.", exc_info=True)

    def skipped(item, detail):
        return CommandResult(
            alias=item.alias,
            session_id=item.session_id,
            hostname=item.hostname,
            device_type=item.device_type,
            command=item.command,
            status=STATUS_SKIPPED,
            risk=item.risk,
            detail=detail,
        )

    def run_session(session_id, session_items):
        """Run one session's commands in order, stopping on a fatal status."""
        out = []
        for item in session_items:
            if cancel_event is not None and cancel_event.is_set():
                out.append(skipped(item, "Cancelled."))
                continue
            if time.monotonic() >= stop_at:
                out.append(skipped(item, "Batch deadline exceeded."))
                continue

            with budget_lock:
                remaining = budget["left"]
            if remaining <= 0:
                out.append(
                    skipped(item, "Output budget for this turn is exhausted.")
                )
                continue

            sess = get_session(session_id)
            result = run_one(
                sess,
                item,
                notify=notify,
                cancel_event=cancel_event,
                output_budget=min(MAX_OUTPUT_CHARS, remaining),
            )
            out.append(result)

            with budget_lock:
                budget["left"] -= len(result.output)

            if result.status in _TERMINAL_STATUSES:
                # No point issuing further commands to a gone / wedged /
                # wrong-mode session; report the rest honestly.
                for rest in session_items[session_items.index(item) + 1 :]:
                    out.append(
                        skipped(
                            rest,
                            f"Skipped after '{result.status}' on this "
                            f"session.",
                        )
                    )
                break

            time.sleep(_INTER_COMMAND_GAP)
        return out

    ordered = []
    workers = max(1, min(max_workers, len(grouped)))
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="ai-exec"
    ) as pool:
        futures = {
            pool.submit(run_session, sid, sitems): sid
            for sid, sitems in grouped.items()
        }
        collected = {}
        for future in as_completed(futures):
            sid = futures[future]
            try:
                collected[sid] = future.result()
            except Exception:
                logger.exception("Session batch crashed for %s.", sid)
                collected[sid] = [
                    skipped(item, "Execution thread crashed.")
                    for item in grouped[sid]
                ]
            if progress:
                for result in collected[sid]:
                    try:
                        progress(result.progress())
                    except Exception:
                        logger.debug(
                            "Progress callback failed.", exc_info=True
                        )

    # Reassemble in plan order so the model sees a predictable sequence.
    cursor = dict.fromkeys(grouped, 0)
    for item in items:
        sid = item.session_id
        bucket = collected.get(sid, [])
        index = cursor[sid]
        if index < len(bucket):
            ordered.append(bucket[index])
            cursor[sid] = index + 1
    return ordered


# ---------------------------------------------------------------------------
# Optional prep — widen the terminal so `show` output does not wrap
# ---------------------------------------------------------------------------
_WIDTH_COMMANDS = {
    "cisco": "terminal width 511",
    "arista": "terminal width 32767",
    "hp": "screen-length disable",
}


def width_command(device_type):
    """Return an allowlisted width command for this platform, or ``None``."""
    from .policy import family_for

    return _WIDTH_COMMANDS.get(family_for(device_type))


def ensure_width(sess, item_factory, notify=None, cancel_event=None):
    """Widen the terminal once per session, so wrapped output does not confuse
    the model. Returns a :class:`CommandResult` or ``None`` if not applicable.

    ``item_factory(command)`` builds a PlanItem so this module stays free of
    policy imports at call time.
    """
    if not sess or sess.get("ai_width_set"):
        return None
    command = width_command(sess.get("device_type", ""))
    if not command:
        return None
    result = run_one(
        sess,
        item_factory(command),
        notify=notify,
        cancel_event=cancel_event,
        timeout=10.0,
    )
    if result.status in (STATUS_OK, STATUS_ERROR):
        sess["ai_width_set"] = True  # do not retry on a device that refused
    return result
