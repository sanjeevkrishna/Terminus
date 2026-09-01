"""Test doubles for the executor: a scripted channel and a read-loop pump.

``FakeChannel`` replays byte streams in response to sent commands, so the
completion detectors and capture machinery can be exercised without a device.
``Pump`` reproduces the exact three-line fan-out that
:func:`terminus.sockets.read_output` performs — monitor, capture, client — so
the tests validate the real contract rather than a paraphrase of it.

File path: tests/fakes.py
"""

import re
import threading
import time


class FakeChannel:
    """A scripted stand-in for a Paramiko channel or PTY wrapper.

    *responses* maps a command (exact string or compiled regex) to either:

    * ``str``                     — emitted, then the prompt
    * ``[(delay, text), ...]``    — emitted piecewise, for chunk-boundary tests
    * ``callable(channel, cmd)``  — full control; must emit its own prompt

    A command with no entry uses *default_response*, or an unknown-command
    error when that is ``None``.
    """

    def __init__(
        self,
        prompt="switch#",
        responses=None,
        echo=True,
        default_response=None,
        posix=False,
        emit_prompt=True,
    ):
        self.prompt = prompt
        self.responses = dict(responses or {})
        self.echo = echo
        self.default_response = default_response
        self.posix = posix
        self.sent = []
        self.closed = False
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._timers = []
        if emit_prompt:
            self.emit(prompt)

    # -- scripting -----------------------------------------------------------
    def emit(self, text, delay=0.0):
        """Queue *text* for delivery, optionally after *delay* seconds."""
        data = text.encode() if isinstance(text, str) else text
        if delay <= 0:
            self._append(data)
            return
        timer = threading.Timer(delay, self._append, args=(data,))
        timer.daemon = True
        self._timers.append(timer)
        timer.start()

    def _append(self, data):
        with self._lock:
            self._buf.extend(data)

    def _lookup(self, command):
        if command in self.responses:
            return self.responses[command]
        for key, value in self.responses.items():
            if isinstance(key, re.Pattern) and key.search(command):
                return value
        return self.default_response

    # -- channel interface ---------------------------------------------------
    def send(self, payload):
        self.sent.append(payload)
        if payload == "\x03":  # Ctrl-C
            self.emit(f"^C\r\n{self.prompt}")
            return len(payload)
        if not payload.endswith("\r"):  # paging answer, etc.
            return len(payload)

        raw = payload[:-1]
        sentinel = None
        if self.posix:
            match = re.search(r"\s*;\s*echo\s+(__TERMINUS_\w+__)$", raw)
            if match:
                sentinel = match.group(1)
                raw = raw[: match.start()]

        command = raw.strip()
        if self.echo:
            self.emit(f"{payload[:-1]}\r\n")

        response = self._lookup(command)
        if response is None:
            self.emit("% Invalid input detected at '^' marker.\r\n")
            self._finish(sentinel)
            return len(payload)

        if callable(response):
            response(self, command)
            return len(payload)

        if isinstance(response, str):
            if response:
                self.emit(
                    response if response.endswith("\n") else response + "\r\n"
                )
            self._finish(sentinel)
            return len(payload)

        elapsed = 0.0
        for delay, text in response:
            elapsed += delay
            self.emit(text, delay=elapsed)
        self._finish(sentinel, delay=elapsed)
        return len(payload)

    def _finish(self, sentinel, delay=0.0):
        tail = delay + 0.08 if delay else 0.0
        if sentinel:
            self.emit(f"{sentinel}\r\n", delay=tail)
        self.emit(self.prompt, delay=tail)

    def recv_ready(self):
        with self._lock:
            return bool(self._buf)

    def recv(self, size=65536):
        with self._lock:
            out = bytes(self._buf[:size])
            del self._buf[:size]
        return out

    def send_ready(self):
        return not self.closed

    def resize_pty(self, width=80, height=24):
        pass

    def settimeout(self, _timeout):
        pass

    def close(self):
        self.closed = True
        for timer in self._timers:
            timer.cancel()


class Pump(threading.Thread):
    """Reproduces the socket read loop's fan-out against a fake session."""

    def __init__(self, sess, sink=None):
        super().__init__(daemon=True, name="test-pump")
        self.sess = sess
        self.sink = sink if sink is not None else []
        self._halt = threading.Event()

    def run(self):
        channel = self.sess["channel"]
        while not self._halt.is_set():
            if not channel.recv_ready():
                if channel.closed:
                    break
                time.sleep(0.01)
                continue
            data = channel.recv(65536)
            if not data:
                continue
            # --- the three lines added to read_output() ---
            self.sess["monitor"].note(data)
            capture = self.sess.get("capture")
            if capture is not None:
                capture.feed(data)
            # ----------------------------------------------
            self.sink.append(data)

    def stop(self):
        self._halt.set()
        self.join(timeout=2.0)
