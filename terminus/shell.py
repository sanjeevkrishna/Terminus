"""Local shell PTY adapter.

Spawns the local shell in a pseudo-terminal and exposes a channel-like
interface (recv_ready / recv / send / resize_pty / closed) matching the
SSH channel used by services.py, so the socket handlers can treat local
shells and SSH sessions identically.

Windows uses pywinpty (ConPTY); POSIX uses the stdlib pty module.

File path: terminus/shell.py
"""
import logging
import os
import sys

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"


class LocalShellChannel:
    """Channel-like wrapper around a local PTY."""

    def __init__(self, shell):
        self._closed = False
        if _IS_WINDOWS:
            self._init_windows(shell)
        else:
            self._init_posix(shell)

    # ------------------------------------------------------------------ Windows
    def _init_windows(self, shell):
        import winpty  # pywinpty
        self._backend = "win"
        self._pty = winpty.PtyProcess.spawn(shell, dimensions=(24, 80))

    # -------------------------------------------------------------------- POSIX
    def _init_posix(self, shell):
        import pty
        import fcntl
        self._backend = "posix"
        self._fcntl = fcntl
        pid, fd = pty.fork()
        if pid == 0:
            # Child: replace with the shell.
            os.execvp(shell, [shell])
        # Parent.
        self._pid = pid
        self._fd = fd
        # Non-blocking reads.
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    # ---------------------------------------------------------------- interface
    def recv_ready(self):
        if _IS_WINDOWS:
            return self._pty.isalive()
        return not self._closed

    def recv(self, _size=65536):
        """Return bytes read from the shell (may be b'' if nothing ready)."""
        try:
            if _IS_WINDOWS:
                if not self._pty.isalive():
                    self._closed = True
                    return b""
                data = self._pty.read(65536)  # str
                return data.encode(errors="ignore") if data else b""
            # POSIX
            try:
                data = os.read(self._fd, 65536)
                if not data:
                    self._closed = True
                return data
            except BlockingIOError:
                return b""
            except OSError:
                self._closed = True
                return b""
        except EOFError:
            self._closed = True
            return b""
        except Exception:
            self._closed = True
            return b""

    def send_ready(self):
        return not self._closed

    def send(self, data):
        if isinstance(data, bytes):
            text = data.decode(errors="ignore")
        else:
            text = data
        try:
            if _IS_WINDOWS:
                self._pty.write(text)
            else:
                os.write(self._fd, text.encode())
        except Exception:
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
            pass

    @property
    def closed(self):
        if _IS_WINDOWS:
            return self._closed or not self._pty.isalive()
        return self._closed

    def settimeout(self, _t):
        pass  # no-op; both backends are non-blocking

    def close(self):
        self._closed = True
        try:
            if _IS_WINDOWS:
                self._pty.terminate(force=True)
            else:
                os.close(self._fd)
        except Exception:
            pass


class LocalShellConn:
    """Minimal conn-like wrapper so sockets.py can call .disconnect()."""

    def __init__(self, channel, label):
        self.channel = channel
        self.base_prompt = label
        self.device_type = "local-shell"

    def disconnect(self):
        self.channel.close()


def open_local_shell(shell, label, logfile=None, tag=""):
    """Open a local shell PTY.

    Returns ``(conn, channel, base_prompt, tee)`` mirroring
    ``services.open_terminus`` so the socket layer treats both uniformly.
    """
    from datetime import datetime

    channel = LocalShellChannel(shell)
    base_prompt = f"Local: {label}"
    conn = LocalShellConn(channel, base_prompt)

    tee = None
    if logfile:
        try:
            tee = open(logfile, "ab", buffering=0)
            banner = (
                "=" * 60 + "\n"
                " Terminus Local Shell Log\n"
                f" Shell  : {shell}\n"
                f" Started: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                + "=" * 60 + "\n\n"
            )
            tee.write(banner.encode())
        except OSError as exc:
            logger.warning("[%s] logfile open failed: %s", tag, exc)

    return conn, channel, base_prompt, tee