"""Local shell PTY adapter.

Spawns a shell in a pseudo-terminal and exposes the same channel interface
the SSH layer uses (``recv_ready`` / ``recv`` / ``send`` / ``resize_pty`` /
``closed``), so :mod:`terminus.sockets` treats local shells and SSH sessions
identically.

Windows uses pywinpty (ConPTY); POSIX uses the stdlib ``pty`` module.

Design note — why a pump thread on Windows: pywinpty's ``read()`` is
non-blocking and ConPTY must be drained promptly. Polling it from the shared
socket loop starves the GIL and output stalls until unrelated I/O forces a
context switch. A dedicated reader thread feeding a queue is the standard
approach (and what every serious ConPTY consumer does).

File path: terminus/shell.py
"""

import logging
import os
import queue
import sys
import threading
import time

from .logbanner import open_session_logfile

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

_PTY_ROWS = 50
_PTY_COLS = 200

# ConPTY negotiates win32-input-mode, which makes xterm.js encode keystrokes
# as verbose escape sequences instead of plain characters. Strip the request
# so the terminal never enables it.
_WIN32_INPUT_MODE = b"\x1b[?9001h"


class LocalShellChannel:
    """Channel-like wrapper around a local PTY."""

    _READ_BYTES = 65536
    _READ_TIMEOUT = 0.05  # how long a consumer read may wait
    _PUMP_IDLE = 0.005  # pump backoff when the PTY is quiet
    _START_GRACE = 2.0  # ignore "not alive" this long after spawn

    def __init__(self, shell, cwd=None, env=None):
        self._closed = False
        self._buf = bytearray()  # bytes read but not yet consumed
        self._started = time.monotonic()
        self._queue = queue.Queue()  # Windows: pump → consumer
        self._pump_thread = None

        if _IS_WINDOWS:
            self._spawn_windows(shell, cwd, env)
            self._pump_thread = threading.Thread(
                target=self._pump, name="pty-pump", daemon=True
            )
            self._pump_thread.start()
        else:
            self._spawn_posix(shell, cwd, env)

    # -- spawn ---------------------------------------------------------------
    def _spawn_windows(self, shell, cwd=None, env=None):
        import winpty

        kwargs = {"dimensions": (_PTY_ROWS, _PTY_COLS)}
        if cwd:
            kwargs["cwd"] = cwd
        if env:
            kwargs["env"] = env
        try:
            self._pty = winpty.PtyProcess.spawn(shell, **kwargs)
        except TypeError:
            # Older pywinpty builds accept neither cwd nor env.
            logger.debug("pywinpty ignored cwd/env; spawning plain.")
            self._pty = winpty.PtyProcess.spawn(
                shell, dimensions=(_PTY_ROWS, _PTY_COLS)
            )

    def _spawn_posix(self, shell, cwd=None, env=None):
        import fcntl
        import pty

        self._fcntl = fcntl
        pid, fd = pty.fork()
        if pid == 0:
            try:
                if cwd:
                    try:
                        os.chdir(cwd)
                    except OSError:
                        pass
                if env:
                    os.execvpe(shell, [shell], env)
                os.execvp(shell, [shell])
            except BaseException:
                pass
            finally:
                os._exit(127)  # conventional "command not executable"
        self._pid = pid
        self._fd = fd
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    # -- liveness -----------------------------------------------------------
    def _alive(self):
        """Whether the child is still running (tolerant during startup).

        ConPTY can briefly report a process as dead while it is still being
        set up, which would otherwise end the session before the shell has
        printed its first prompt.
        """
        if time.monotonic() - self._started < self._START_GRACE:
            return True
        try:
            if _IS_WINDOWS:
                return bool(self._pty.isalive())
            pid, _ = os.waitpid(self._pid, os.WNOHANG)
            return pid == 0
        except ChildProcessError:
            return False
        except Exception:
            logger.debug("Liveness check failed.", exc_info=True)
            return False

    # -- Windows pump thread ------------------------------------------------
    def _pump(self):
        """Drain the PTY into a queue until the child exits."""
        while not self._closed:
            try:
                data = self._pty.read(self._READ_BYTES)
            except EOFError:
                break
            except Exception:
                logger.debug("PTY pump read failed.", exc_info=True)
                break

            if data:
                self._queue.put(data.encode(errors="ignore"))
                continue

            # Quiet: back off so this thread does not spin on the GIL.
            time.sleep(self._PUMP_IDLE)
            if not self._alive():
                break

        self._closed = True
        self._queue.put(b"")  # wake a waiting consumer
        logger.debug("PTY pump finished.")

    # -- read path ----------------------------------------------------------
    def _read_once(self):
        """Return whatever is available now; ``b''`` if nothing arrived."""
        if _IS_WINDOWS:
            try:
                return self._queue.get(timeout=self._READ_TIMEOUT)
            except queue.Empty:
                return b""

        if self._closed:
            return b""
        try:
            import select

            ready, _, _ = select.select([self._fd], [], [], self._READ_TIMEOUT)
            if not ready:
                return b""
            data = os.read(self._fd, self._READ_BYTES)
            if not data and not self._alive():
                self._closed = True
            return data
        except BlockingIOError:
            return b""
        except OSError:
            if not self._alive():
                self._closed = True
            return b""
        except Exception:
            logger.debug("PTY read failed.", exc_info=True)
            self._closed = True
            return b""

    # -- channel interface --------------------------------------------------
    def recv_ready(self):
        """True when bytes are available, waiting briefly to find out.

        A PTY has no cheap readiness test, so this performs a bounded read and
        buffers the result — keeping the socket layer's loop reactive without
        busy-spinning.
        """
        if self._buf:
            return True
        chunk = self._read_once()
        if chunk:
            self._buf.extend(chunk)
            return True
        return False

    def recv(self, size=_READ_BYTES):
        """Return up to *size* buffered bytes."""
        if not self._buf:
            chunk = self._read_once()
            if chunk:
                self._buf.extend(chunk)
        if not self._buf:
            return b""

        # Hold back a short tail when the buffer ends mid-escape, so the
        # win32-input-mode request cannot be split across two returns and slip
        # past the filter below.
        take = min(size, len(self._buf))
        if take == len(self._buf):
            tail = self._partial_escape_len(self._buf)
            if tail and tail < take:
                take -= tail

        out = bytes(self._buf[:take])
        del self._buf[:take]
        return out.replace(_WIN32_INPUT_MODE, b"")

    @staticmethod
    def _partial_escape_len(buf):
        """Length of a trailing partial _WIN32_INPUT_MODE prefix, if any."""
        for length in range(len(_WIN32_INPUT_MODE) - 1, 0, -1):
            if (
                length <= len(buf)
                and buf[-length:] == _WIN32_INPUT_MODE[:length]
            ):
                return length
        return 0

    def send_ready(self):
        return not self._closed

    def send(self, data):
        text = (
            data.decode(errors="ignore") if isinstance(data, bytes) else data
        )
        try:
            if _IS_WINDOWS:
                self._pty.write(text)
            else:
                os.write(self._fd, text.encode())
        except Exception:
            logger.debug("PTY write failed.", exc_info=True)
            self._closed = True

    def resize_pty(self, width=80, height=24):
        try:
            if _IS_WINDOWS:
                self._pty.setwinsize(height, width)
            else:
                import struct
                import termios

                winsize = struct.pack("HHHH", height, width, 0, 0)
                self._fcntl.ioctl(self._fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            logger.debug("PTY resize failed.", exc_info=True)

    @property
    def closed(self):
        """Closed once shut down *and* fully drained.

        Buffered or queued output keeps the channel open so the reader can
        emit the shell's final bytes before the session is torn down.
        """
        if self._buf:
            return False
        if _IS_WINDOWS and not self._queue.empty():
            return False
        return self._closed or not self._alive()

    def settimeout(self, _timeout):
        """No-op: reads are bounded internally (socket-channel parity)."""

    def close(self):
        self._closed = True
        try:
            if _IS_WINDOWS:
                self._pty.terminate(force=True)
            else:
                self._close_posix()
        except Exception:
            logger.debug("PTY close failed.", exc_info=True)

    def _close_posix(self):
        """Close the master fd, then signal and reap the child.

        Closing the fd sends SIGHUP to the foreground process group, which is
        enough for a well-behaved shell. One that ignores it would otherwise
        linger as an orphan, so escalate and reap explicitly.
        """
        import signal

        try:
            os.close(self._fd)
        except OSError:
            pass

        pid = getattr(self, "_pid", None)
        if not pid:
            return

        for sig, grace in (
            (signal.SIGHUP, 0.3),
            (signal.SIGTERM, 0.3),
            (signal.SIGKILL, 0.5),
        ):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break  # already gone
            except OSError:
                break
            if self._reap(pid, grace):
                return

        self._reap(pid, 0.0)  # last attempt, non-blocking

    @staticmethod
    def _reap(pid, timeout):
        """Wait up to *timeout* for *pid* to exit. True once reaped."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                done, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return True  # reaped elsewhere
            except OSError:
                return True
            if done:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


class LocalShellConn:
    """Minimal conn-like wrapper so sockets.py can call ``disconnect()``."""

    def __init__(self, channel, label):
        self.channel = channel
        self.base_prompt = label
        self.device_type = "local-shell"

    def disconnect(self):
        self.channel.close()

    # Alias, matching TerminalSession.
    close = disconnect


def _clean_env():
    """Return the environment with any virtualenv activation undone.

    The PTY inherits Terminus's environment, so a shell spawned from inside a
    virtualenv would appear activated (VIRTUAL_ENV set, the venv's Scripts/bin
    prepended to PATH). Strip that so the shell starts as the user's own.
    """
    env = os.environ.copy()

    venv = env.pop("VIRTUAL_ENV", None)
    for key in (
        "VIRTUAL_ENV_PROMPT",
        "CONDA_DEFAULT_ENV",
        "CONDA_PREFIX",
        "PYTHONHOME",
        "PYTHONPATH",
        "_OLD_VIRTUAL_PROMPT",
        "_OLD_VIRTUAL_PYTHONHOME",
    ):
        env.pop(key, None)

    original = env.pop("_OLD_VIRTUAL_PATH", None)
    if original:
        env["PATH"] = original
    elif venv:
        venv_norm = os.path.normcase(os.path.normpath(venv))
        parts = [
            p
            for p in env.get("PATH", "").split(os.pathsep)
            if p
            and not os.path.normcase(os.path.normpath(p)).startswith(venv_norm)
        ]
        env["PATH"] = os.pathsep.join(parts)

    return env


def open_local_shell(shell, label, logfile=None, tag="", cwd=None):
    """Open a local shell PTY.

    *cwd* defaults to the user's home so the shell behaves like one launched
    from the desktop rather than inheriting the app's working directory.

    Returns ``(conn, channel, base_prompt, tee)`` mirroring
    ``services.open_terminus`` so the socket layer treats both uniformly.
    """
    start_dir = cwd or os.path.expanduser("~")
    if not os.path.isdir(start_dir):
        start_dir = None  # let the OS decide

    logger.debug("Local shell spawn -> %s (cwd=%s)", shell, start_dir)
    channel = LocalShellChannel(shell, cwd=start_dir, env=_clean_env())
    conn = LocalShellConn(channel, f"Local: {label}")

    tee = (
        open_session_logfile(
            logfile,
            "Terminus Local Shell Log",
            {"Shell": shell, "Dir": start_dir or os.getcwd()},
            tag=tag,
        )
        if logfile
        else None
    )
    return conn, channel, conn.base_prompt, tee
