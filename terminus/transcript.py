"""Session output capture: disk log + in-memory transcript.

Extracted from :mod:`terminus.sockets` so the socket layer stays focused on
transport. Two consumers share one cleaned byte stream:

* :class:`SessionLog`   — the on-disk log file (adaptive emulate/bulk).
* :class:`SessionTranscript` — a rolling in-memory view for AI context,
  with no disk I/O and no snapshot side effects.

Both distinguish *committed* text (final, append-only) from *provisional*
text (the current emulated screen, which will be rewritten as output
scrolls). That distinction is what lets a live log be read repeatedly
without accumulating duplicated screen dumps.

File path: terminus/transcript.py
"""

import logging
import os
import queue
import re
import threading
import time
from collections import deque

import pyte

logger = logging.getLogger(__name__)

# -- session log emulator ---------------------------------------------------
_TERM_ROWS = 50
_TERM_COLS = 200
_TERM_HISTORY = 20000  # headroom so one feed can never overflow it
_FEED_SLICE = 8192  # bytes per emulation step
_BULK_QUEUE_DEPTH = 24  # ~1.5 MB queued before abandoning emulation
_BULK_EXIT_IDLE = 0.4  # seconds idle before resuming emulation
_QUEUE_GET_TIMEOUT = 0.2
_CLOSE_DRAIN_TIMEOUT = 120.0
_CLOSE_JOIN_TIMEOUT = 30.0
_SNAPSHOT_DRAIN_TIMEOUT = 3.0
_QUEUE_MAX_CHUNKS = 4096

# -- transcript -------------------------------------------------------------
TRANSCRIPT_MAX_CHARS = 512 * 1024
_MARK_PREFIX = "[Terminus AI]"

# -- escape handling --------------------------------------------------------
# Erase sequences the bulk log path must apply rather than strip.
_CURSOR_BACK_RE = re.compile(rb"\x1b\[(\d*)D")
_ERASE_EOL_RE = re.compile(rb"\x1b\[0?K")

# \x5c is a literal backslash (the ST terminator), written this way so the
# pattern survives copy/paste intact.
_ANSI_RE = re.compile(
    rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\x5c)"  # OSC, BEL- or ST-terminated
    rb"|\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI
    rb"|\x1b[@-Z\x5c-_]"  # other two-char escapes
    rb"|[\x00-\x07\x0b\x0c\x0e-\x1f\x7f]"  # stray control chars
)
_MULTI_NL_RE = re.compile(rb"\n{3,}")


# ---------------------------------------------------------------------------
# Raw-mode cleanup (bulk path — pyte is bypassed)
# ---------------------------------------------------------------------------
def apply_erasures(data: bytes) -> bytes:
    """Apply terminal erase semantics to a raw byte run.

    Handles BS (``\x08``), CUB (``ESC[nD``) and EL (``ESC[K``). Without this,
    a character typed then deleted would survive in the log because the erase
    escape is merely stripped.
    """

    def _cub(match):
        return b"\x08" * int(match.group(1) or 1)

    data = _CURSOR_BACK_RE.sub(_cub, data)
    if b"\x08" not in data and not _ERASE_EOL_RE.search(data):
        return data

    out = bytearray()
    line_start = 0  # index in `out` where the current line begins
    i = 0
    while i < len(data):
        byte = data[i]
        if byte == 0x08:  # backspace
            if len(out) > line_start:
                out.pop()
            i += 1
            continue
        match = _ERASE_EOL_RE.match(data, i)
        if byte == 0x1B and match:
            # Erase-to-EOL is a no-op for an append-only buffer.
            i = match.end()
            continue
        out.append(byte)
        if byte == 0x0A:  # newline
            line_start = len(out)
        i += 1
    return bytes(out)


def clean_raw(data: bytes) -> bytes:
    """Cheap cleanup for bulk output: erasures, ANSI, newline normalising."""
    data = apply_erasures(data)
    data = _ANSI_RE.sub(b"", data)
    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return _MULTI_NL_RE.sub(b"\n\n", data)


def clean_text(data: bytes) -> str:
    """Clean *data* and decode it to text."""
    return clean_raw(data).decode("utf-8", errors="replace")


_TAIL_BYTES = 2048


# ---------------------------------------------------------------------------
# In-memory transcript
# ---------------------------------------------------------------------------
class SessionTranscript:
    """Rolling cleaned view of one session, for AI context.

    Mirrors :class:`SessionLog`'s output but keeps it in memory and honours
    the committed/provisional split properly: the current screen occupies a
    single replaceable slot instead of being appended, so reading the
    transcript repeatedly never duplicates content.
    """

    def __init__(self, max_chars=TRANSCRIPT_MAX_CHARS):
        self._max_chars = max_chars
        self._committed = deque()
        self._committed_chars = 0
        self._screen = ""
        self._marks = deque(maxlen=200)
        self._dropped = False
        self._lock = threading.Lock()

    # -- writers (called by SessionLog) --------------------------------------
    def commit(self, text):
        """Append final text that will never be rewritten."""
        if not text:
            return
        with self._lock:
            self._committed.append(text)
            self._committed_chars += len(text)
            self._trim()

    def set_screen(self, text):
        """Replace the provisional (currently-displayed) region."""
        with self._lock:
            self._screen = text or ""

    def add_mark(self, label):
        """Record an AI-executed command boundary at the current offset."""
        with self._lock:
            self._marks.append(
                {
                    "offset": self._committed_chars,
                    "label": label,
                    "at": time.time(),
                }
            )

    def _trim(self):
        """Drop oldest fragments once over budget (caller holds the lock)."""
        while (
            self._committed_chars > self._max_chars
            and len(self._committed) > 1
        ):
            dropped = self._committed.popleft()
            self._committed_chars -= len(dropped)
            self._dropped = True

    # -- reader --------------------------------------------------------------
    def read(self, limit=None):
        """Return ``(text, truncated)`` — committed history plus the screen."""
        with self._lock:
            parts = list(self._committed)
            screen = self._screen
            over_budget = self._dropped

        text = "".join(parts)
        if screen:
            text = f"{text}\n{screen}" if text else screen

        truncated = over_budget
        if limit and len(text) > limit:
            text = text[-limit:]
            newline = text.find("\n")
            if 0 <= newline < 200:  # drop a partial first line
                text = text[newline + 1 :]
            truncated = True
        return text, truncated

    def marks(self):
        with self._lock:
            return list(self._marks)

    def clear(self):
        with self._lock:
            self._committed.clear()
            self._committed_chars = 0
            self._screen = ""
            self._marks.clear()
            self._dropped = False


# ---------------------------------------------------------------------------
# Session logging
# ---------------------------------------------------------------------------
class SessionLog:
    """Tees a session to disk (and a transcript), adapting to throughput.

    Two modes, chosen automatically:

    * **emulate** — bytes go through a headless pyte terminal, so backspaces,
      cursor moves and redraws are applied exactly as xterm shows them. Used
      for interactive work, where fidelity matters.
    * **bulk** — engaged when the worker falls behind (queue depth exceeds
      ``_BULK_QUEUE_DEPTH``). Bytes are ANSI-stripped and appended directly,
      which is orders of magnitude faster. Large ``show`` output contains no
      interactive edits, so the result is equivalent.

    The mode reverts to *emulate* once output goes idle or the user types.

    Screen dumps are written as a *provisional tail*: the file offset is
    remembered, and the next committed write truncates back to it. Reading a
    live log repeatedly therefore cannot accumulate duplicated screens.

    Known artifact: at the emulate→bulk switch one line may be truncated —
    pyte has already written the partially-echoed current line, so the raw
    path skips the remainder rather than writing that line twice.
    """

    _MODE_EMULATE = "emulate"
    _MODE_BULK = "bulk"

    def __init__(self, tee, transcript=None):
        self._tee = tee
        self._transcript = transcript
        self._active = bool(tee or transcript)
        self._screen = pyte.HistoryScreen(
            _TERM_COLS, _TERM_ROWS, history=_TERM_HISTORY, ratio=0.5
        )
        self._stream = pyte.ByteStream(self._screen)
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._stop = object()  # sentinel
        self._worker = None
        self._last_screen = None  # dedupe repeated screen dumps
        self._mode = self._MODE_EMULATE
        self._raw_tail = bytearray()  # partial line held back in bulk mode
        self._skip_to_newline = False  # avoid duplicating a partial line
        self._provisional_at = None  # file offset of the replaceable tail
        self._interactive = False  # set by note_input(), lock-free
        self._dropped_chunks = 0
        self._dropped_bytes = 0
        if self._active:
            self._worker = threading.Thread(
                target=self._run, name="session-log", daemon=True
            )
            self._worker.start()

    # -- producer (called from the read loop; never blocks) ------------------
    def feed(self, data: bytes):
        """Queue raw output bytes for logging.

        Never blocks the read loop: if the worker has fallen catastrophically
        behind, bytes are dropped and accounted for rather than growing the
        queue without bound. Bulk mode normally prevents this.
        """
        if not self._active:
            return
        if self._queue.qsize() >= _QUEUE_MAX_CHUNKS:
            self._dropped_chunks += 1
            self._dropped_bytes += len(data)
            if self._dropped_chunks == 1 or self._dropped_chunks % 500 == 0:
                logger.warning(
                    "Session log backlog full — dropped %d chunk(s), %d byte(s).",
                    self._dropped_chunks,
                    self._dropped_bytes,
                )
            return
        self._queue.put(data)

    def write_marker(self, text):
        """Queue an out-of-band marker, ordered against device output.

        Markers must not be written straight to the file: the worker is
        asynchronous, so a direct write would land out of order relative to
        the command echo that follows it.
        """
        if self._active:
            self._queue.put(("marker", text))

    def resize(self, cols, rows):
        """Match the emulated screen to the live terminal geometry."""
        if not self._active:
            return
        with self._lock:
            try:
                self._screen.resize(rows, cols)
            except Exception:
                logger.debug("pyte resize failed.", exc_info=True)

    def note_input(self):
        """User typed — interactive again, so restore full emulation.

        Deliberately lock-free: the worker can hold ``_lock`` for the length
        of a full pyte feed, and blocking a Socket.IO handler thread during
        heavy output is exactly when the user is reaching for Ctrl-C.
        """
        if self._active and self._mode == self._MODE_BULK:
            self._interactive = True

    # -- worker --------------------------------------------------------------
    def _run(self):
        idle_since = None
        while True:
            try:
                item = self._queue.get(timeout=_QUEUE_GET_TIMEOUT)
            except queue.Empty:
                if self._mode == self._MODE_BULK:
                    if self._interactive:
                        with self._lock:
                            self._exit_bulk()
                        idle_since = None
                    elif idle_since is None:
                        idle_since = time.monotonic()
                    elif time.monotonic() - idle_since >= _BULK_EXIT_IDLE:
                        with self._lock:
                            self._exit_bulk()
                        idle_since = None
                continue

            idle_since = None
            if item is self._stop:
                self._queue.task_done()
                return

            with self._lock:
                try:
                    if isinstance(item, tuple) and item[0] == "marker":
                        self._write_marker_locked(item[1])
                    else:
                        if self._interactive and self._mode == self._MODE_BULK:
                            self._exit_bulk()
                        if (
                            self._mode == self._MODE_EMULATE
                            and self._queue.qsize() >= _BULK_QUEUE_DEPTH
                        ):
                            self._enter_bulk()
                        if self._mode == self._MODE_BULK:
                            self._write_bulk(item)
                        else:
                            self._feed_emulated(item)
                except Exception:
                    logger.debug(
                        "session log feed error (ignored).", exc_info=True
                    )
            self._queue.task_done()

    def _drain(self, timeout):
        """Block until every queued chunk has been processed (bounded).

        ``Queue.join()`` is the real barrier — ``empty()`` would return true
        while the worker is still mid-feed.
        """
        if not self._worker:
            return
        done = threading.Event()

        def waiter():
            self._queue.join()
            done.set()

        threading.Thread(target=waiter, daemon=True).start()
        done.wait(timeout)

    # -- mode handling (under self._lock) ------------------------------------
    def _enter_bulk(self):
        logger.debug(
            "Session log → bulk mode (backlog %d).", self._queue.qsize()
        )
        self._flush_history()
        self._write_screen(force=True, committed=True)
        self._reset_screen()
        self._mode = self._MODE_BULK
        self._skip_to_newline = True

    def _exit_bulk(self):
        logger.debug("Session log → emulate mode.")
        self._flush_raw_tail()
        self._reset_screen()
        self._mode = self._MODE_EMULATE
        self._interactive = False

    def _reset_screen(self):
        try:
            self._screen.reset()
        except Exception:
            logger.debug("pyte reset failed.", exc_info=True)
        history = getattr(self._screen, "history", None)
        for side in ("top", "bottom"):
            deq = getattr(history, side, None) if history else None
            if deq is not None:
                deq.clear()
        self._last_screen = None

    # -- writers (under self._lock) -----------------------------------------
    def _write_marker_locked(self, text):
        """Anchor a marker so pyte can never reflow or dedupe over it."""
        self._flush_history()
        self._write_screen(force=True, committed=True)
        self._reset_screen()
        self._commit(text if text.endswith("\n") else text + "\n")
        if self._transcript:
            self._transcript.add_mark(text.strip())

    def _feed_emulated(self, data: bytes):
        for start in range(0, len(data), _FEED_SLICE):
            self._stream.feed(data[start : start + _FEED_SLICE])
            self._flush_history()

    def _write_bulk(self, data: bytes):
        self._raw_tail.extend(data)
        nl = self._raw_tail.rfind(b"\n")
        if nl == -1:
            return
        complete = bytes(self._raw_tail[: nl + 1])
        del self._raw_tail[: nl + 1]

        if self._skip_to_newline:
            complete = complete[complete.find(b"\n") + 1 :]
            self._skip_to_newline = False
            if not complete:
                return
        self._commit(clean_text(complete))

    def _flush_raw_tail(self):
        if not self._raw_tail:
            return
        tail = bytes(self._raw_tail)
        self._raw_tail.clear()
        if self._skip_to_newline:
            self._skip_to_newline = False
            return
        self._commit(clean_text(tail))

    def _flush_history(self):
        history = getattr(self._screen, "history", None)
        top = getattr(history, "top", None) if history else None
        if not top:
            return
        lines = []
        while top:
            lines.append(self._render_line(top.popleft()))
        self._commit("\n".join(lines) + "\n")

    def _write_screen(self, force=False, committed=False):
        display = list(self._screen.display)
        while display and not display[-1].strip():
            display.pop()
        if not display:
            return
        text = "\n".join(display)
        if not force and text == self._last_screen:
            return
        self._last_screen = text
        if committed:
            self._commit(text + "\n")
        else:
            self._provisional(text + "\n")

    # -- consumers -----------------------------------------------------------
    def snapshot(self):
        """Flush everything known so far, for reading a live log."""
        if not self._active:
            return
        self._drain(_SNAPSHOT_DRAIN_TIMEOUT)
        with self._lock:
            if self._mode == self._MODE_BULK:
                self._flush_raw_tail()
            else:
                self._flush_history()
                self._write_screen()  # provisional — replaceable
        self._flush_file()

    def close(self):
        """Stop the worker, flush the remainder, finalize the log."""
        if not self._active:
            return
        backlog = self._queue.qsize()
        if backlog:
            logger.info("Finishing session log — %d chunk(s) queued.", backlog)
        self._drain(_CLOSE_DRAIN_TIMEOUT)
        self._queue.put(self._stop)
        if self._worker:
            self._worker.join(timeout=_CLOSE_JOIN_TIMEOUT)
        with self._lock:
            if self._mode == self._MODE_BULK:
                self._flush_raw_tail()
            else:
                self._flush_history()
                self._write_screen(force=True, committed=True)
            if self._dropped_chunks:
                self._commit(
                    f"\n[Terminus] {self._dropped_bytes:,} byte(s) in "
                    f"{self._dropped_chunks} chunk(s) were dropped from this "
                    f"log — output exceeded the writer's throughput.\n"
                )
        self._flush_file()

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _render_line(line):
        """Render a pyte line (sparse x → Char mapping) to plain text."""
        if not line:
            return ""
        last = max(line)
        buf = [" "] * (last + 1)
        for x, char in line.items():
            if x <= last:
                buf[x] = char.data
        return "".join(buf).rstrip()

    # -- sinks (under self._lock) -------------------------------------------
    def _commit(self, text):
        """Write final text: drop any provisional tail first."""
        if not text:
            return
        self._drop_provisional()
        self._write_file(text)
        if self._transcript:
            self._transcript.commit(text)

    def _provisional(self, text):
        """Write replaceable text, overwriting any previous provisional tail."""
        if not text:
            return
        if self._provisional_at is None:
            self._mark_provisional()
        else:
            self._truncate_to(self._provisional_at)
        self._write_file(text)
        if self._transcript:
            self._transcript.set_screen(text.rstrip("\n"))

    def _mark_provisional(self):
        if self._tee is None:
            return
        try:
            self._tee.flush()
            self._provisional_at = self._tee.tell()
        except (OSError, ValueError):
            self._provisional_at = None

    def _truncate_to(self, offset):
        if self._tee is None:
            return
        try:
            self._tee.flush()
            self._tee.truncate(offset)
            self._tee.seek(offset)  # truncate does not move the position
        except (OSError, ValueError):
            logger.debug("Could not truncate provisional log tail.")

    def _drop_provisional(self):
        if self._provisional_at is not None:
            self._truncate_to(self._provisional_at)
            self._provisional_at = None
        if self._transcript:
            self._transcript.set_screen("")

    def _write_file(self, text):
        """Write text to the tee, tolerating a handle closed underneath us."""
        if self._tee is None:
            return
        try:
            self._tee.write(text.encode("utf-8", errors="replace"))
        except (OSError, ValueError):
            pass  # closed by a concurrent teardown, or a full disk

    def _flush_file(self):
        """Flush and fsync, so a log opened right now is complete on disk."""
        if self._tee is None:
            return
        try:
            self._tee.flush()
            if callable(getattr(self._tee, "fileno", None)):
                os.fsync(self._tee.fileno())
        except (OSError, ValueError):
            pass


# ---------------------------------------------------------------------------
# Liveness / idle tracking (fed synchronously by the read loop)
# ---------------------------------------------------------------------------
class SessionMonitor:
    """Tracks recently received bytes so preflight can be exact.

    Updated synchronously from the read loop — unlike the transcript, which is
    produced by an asynchronous worker and may lag by hundreds of
    milliseconds. Preflight cannot tolerate that lag.
    """

    def __init__(self):
        self._tail = bytearray()
        self._last_rx = 0.0
        self._lock = threading.Lock()

    def note(self, data: bytes):
        with self._lock:
            self._tail.extend(data)
            if len(self._tail) > _TAIL_BYTES:
                del self._tail[:-_TAIL_BYTES]
            self._last_rx = time.monotonic()

    def idle_for(self):
        """Seconds since the last byte; ``inf`` if nothing has arrived yet."""
        with self._lock:
            if not self._last_rx:
                return float("inf")
            return time.monotonic() - self._last_rx

    def tail_text(self):
        with self._lock:
            raw = bytes(self._tail)
        return clean_text(raw)

    def last_line(self):
        for line in reversed(self.tail_text().splitlines()):
            if line.strip():
                return line.strip()
        return ""
