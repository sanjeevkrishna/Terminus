"""Shared launcher plumbing: logging, port resolution, readiness.

File path: launchers/common.py
"""

import logging
import os
import socket
import sys
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from threading import Thread

from terminus.app import BASE_DIR, HOST, PORT

# Must not live in LOG_DIR — the Logs panel globs *.log there and would list the
# application log alongside real session logs.
APP_LOG_PATH = os.path.join(BASE_DIR, "terminus-app.log")

# Chatty at DEBUG, and rarely what you are looking for.
_NOISY = (
    "httpcore",
    "httpx",
    "urllib3",
    "engineio",
    "socketio",
    "werkzeug",
    "paramiko",
)

# The resolved port, exported so the Werkzeug reloader's child process reuses it
# instead of probing again.
_PORT_ENV = "TERMINUS_RESOLVED_PORT"

logger = logging.getLogger(__name__)

_configured = False


def configure_logging(level=None, to_file=True):
    """Set up root logging. *level* overrides ``TERMINUS_LOG``."""
    global _configured

    name = (level or os.environ.get("TERMINUS_LOG", "INFO")).upper()
    resolved = getattr(logging, name, logging.INFO)

    if _configured:
        return resolved
    _configured = True

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(resolved)

    if sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)

    if to_file:
        try:
            os.makedirs(BASE_DIR, exist_ok=True)
            rotating = RotatingFileHandler(
                APP_LOG_PATH,
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            rotating.setFormatter(formatter)
            root.addHandler(rotating)
        except OSError:
            pass  # stderr only

    for noisy in _NOISY:
        logging.getLogger(noisy).setLevel(max(resolved, logging.INFO))

    return resolved


def port_is_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((HOST, port))
            return True
        except OSError:
            return False


def free_port():
    """Ask the OS for an unused port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


def pick_port(preferred=None):
    """Return *preferred* if free, otherwise an OS-assigned port.

    There is an unavoidable race between probing and binding, so callers must
    verify the server actually came up.
    """
    candidate = preferred or PORT
    return candidate if port_is_free(candidate) else free_port()


def resolve_port(preferred=None):
    """Resolve the port once per launch, stable across a reloader restart.

    The Werkzeug reloader binds the socket in the parent process, then
    re-executes; the child inherits the socket but re-runs the launcher. Probing
    again there would see the original port as taken — because the parent holds
    it — and pick a different one, building the app with an origin allowlist for
    a port it is not actually serving on. Every request would then be refused as
    a bad Host.
    """
    cached = os.environ.get(_PORT_ENV)
    if cached:
        try:
            return int(cached)
        except ValueError:
            pass

    port = pick_port(preferred)
    os.environ[_PORT_ENV] = str(port)
    return port


def wait_until_up(port, timeout=10.0):
    """Block until the server accepts connections, or *timeout* elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((HOST, port)) == 0:
                return True
        time.sleep(0.1)
    return False


def open_browser_when_ready(port, timeout=15.0):
    """Open a browser tab once the server is listening.

    A fixed timer races the first import — netmiko alone takes a second or two.
    """

    def wait():
        if wait_until_up(port, timeout):
            webbrowser.open(f"http://{HOST}:{port}")
        else:
            logger.warning("Server did not come up; not opening a browser.")

    Thread(target=wait, name="terminus-browser", daemon=True).start()


def under_reloader():
    """True in the Werkzeug reloader's child process.

    Used to avoid opening a second browser tab on every code reload.
    """
    return os.environ.get("WERKZEUG_RUN_MAIN") == "true"
