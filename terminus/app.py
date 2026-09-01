"""Terminus — local, desktop-style web SSH terminal.

Application factory + runtime configuration. Creates the ~/.terminus working
directory and a persistent encryption key on import, then exposes
``create_app()`` for the web and desktop launchers.

File path: terminus/app.py
"""

import atexit
import logging
import os
import secrets
import shutil
import sys

from flask import Flask
from flask_socketio import SocketIO

from .paths import resource

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Runtime configuration (constants first, so sibling modules can import them
# without triggering a circular import via create_app()).
# ---------------------------------------------------------------------------
BASE_DIR = os.path.expanduser("~/.terminus")
LOG_DIR = os.path.join(BASE_DIR, "logs")
DB_PATH = os.path.join(BASE_DIR, "terminus.db")
PREFS_PATH = os.path.join(BASE_DIR, "prefs.json")
_KEY_PATH = os.path.join(BASE_DIR, ".key")

HOST = "127.0.0.1"
PORT = 5001

# Regenerated every launch. The page embeds it and the Socket.IO handshake
# requires it, so a second Terminus instance — or any other local process —
# cannot drive this one.
LAUNCH_TOKEN = secrets.token_urlsafe(32)


def allowed_origins(port):
    """Origins permitted to open a Socket.IO connection."""
    return [f"http://{HOST}:{port}", f"http://localhost:{port}"]


def allowed_hosts(port):
    """Host/Origin authorities accepted on HTTP requests."""
    return {f"{HOST}:{port}", f"localhost:{port}"}


def _discover_shells():
    """Return [{id, label, command}] for shells present on this machine."""
    found = []

    if sys.platform == "win32":
        candidates = [
            (
                "powershell",
                "Windows PowerShell",
                os.path.join(
                    os.environ.get("SystemRoot", r"C:\Windows"),  # noqa: SIM112
                    "System32",
                    "WindowsPowerShell",
                    "v1.0",
                    "powershell.exe",
                ),
            ),
            ("pwsh", "PowerShell 7", shutil.which("pwsh")),
            ("cmd", "Command Prompt", os.environ.get("COMSPEC", "cmd.exe")),
            ("wsl", "WSL", shutil.which("wsl")),
        ]
    else:
        candidates = [
            ("bash", "Bash", shutil.which("bash")),
            ("zsh", "Zsh", shutil.which("zsh")),
            ("fish", "Fish", shutil.which("fish")),
            ("sh", "sh", shutil.which("sh")),
        ]

    for shell_id, label, command in candidates:
        if command and (os.path.isfile(command) or shutil.which(command)):
            found.append({"id": shell_id, "label": label, "command": command})

    # Always offer the environment default, even if unlisted above.
    default_cmd = (
        os.environ.get("COMSPEC", "cmd.exe")
        if sys.platform == "win32"
        else os.environ.get("SHELL", "/bin/sh")
    )
    if default_cmd and not any(s["command"] == default_cmd for s in found):
        found.insert(
            0,
            {
                "id": "default",
                "label": f"Default ({os.path.basename(default_cmd)})",
                "command": default_cmd,
            },
        )

    return found


LOCAL_SHELLS = _discover_shells()
LOCAL_SHELL = LOCAL_SHELLS[0]["command"] if LOCAL_SHELLS else "/bin/sh"
LOCAL_SHELL_LABEL = LOCAL_SHELLS[0]["label"] if LOCAL_SHELLS else "shell"


def shell_by_id(shell_id):
    """Look up a discovered shell by id; fall back to the first available."""
    for shell in LOCAL_SHELLS:
        if shell["id"] == shell_id:
            return shell
    return (
        LOCAL_SHELLS[0]
        if LOCAL_SHELLS
        else {
            "id": "default",
            "label": "shell",
            "command": LOCAL_SHELL,
        }
    )


def _load_or_create_secret():
    """Load the on-disk encryption key, generating one on first run."""
    os.makedirs(BASE_DIR, exist_ok=True)
    if os.path.exists(_KEY_PATH):
        with open(_KEY_PATH, encoding="utf-8") as fh:
            return fh.read().strip()

    key = secrets.token_urlsafe(48)
    with open(_KEY_PATH, "w", encoding="utf-8") as fh:
        fh.write(key)
    try:
        os.chmod(_KEY_PATH, 0o600)
    except OSError:
        logger.debug("Could not chmod key file (non-POSIX filesystem).")
    return key


SECRET_KEY = _load_or_create_secret()
os.makedirs(LOG_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------
def create_app(port=PORT):
    """Build and return ``(app, socketio)`` in threading mode.

    *port* must be the port the server will actually bind: the origin
    allowlist and Host check are derived from it.

    ``routes`` and ``sockets`` are imported here rather than at module level.
    Both reach back into this module via ``credentials`` / ``ai.settings``, so a
    module-level import makes ``terminus.app`` the only viable entry point into
    the package — importing any other module first raises ImportError. Deferring
    them keeps every module independently importable, which matters for tests,
    the probe scripts, and a REPL.
    """
    from .routes import register_routes
    from .sockets import register_socket_handlers

    app = Flask(
        __name__,
        template_folder=resource("templates"),
        static_folder=resource("static"),
    )
    app.secret_key = SECRET_KEY
    app.secret_key = SECRET_KEY
    app.config["TERMINUS_PORT"] = port
    app.config["TERMINUS_TOKEN"] = LAUNCH_TOKEN
    app.config["TERMINUS_HOSTS"] = allowed_hosts(port)

    # A wildcard here would let any page you have open in any tab connect to
    # this socket and emit `open_shell` + `input` — arbitrary local code
    # execution. WebSocket handshakes are not constrained by the same-origin
    # policy the way fetch() is, so the browser will not stop it.
    socketio = SocketIO(
        app, async_mode="threading", cors_allowed_origins=allowed_origins(port)
    )

    register_routes(app)
    register_socket_handlers(socketio)
    logger.info(
        "App configured for %s:%s (origin allowlist active).", HOST, port
    )
    return app, socketio


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown_ran = False


def shutdown():
    """Cancel AI conversations, flush logs, close sessions.

    Every worker is a daemon thread and the session-log tee is 256 KB-buffered,
    so without this a window close loses the tail of every log. Conversations
    are dropped first: a turn blocked awaiting approval must be woken and
    abandoned, not left to execute against channels being torn down.

    Idempotent — safe to call from both an atexit hook and an explicit path.
    """
    global _shutdown_ran
    if _shutdown_ran:
        return
    _shutdown_ran = True

    try:
        from .sockets import _finalize_log, get_state
    except Exception:
        logger.debug("Shutdown skipped — package not fully imported.")
        return

    try:
        from . import ai

        for chat_id in list(ai.agent._conversations):
            ai.agent.drop(chat_id)
    except Exception:
        logger.debug("Chat teardown skipped.", exc_info=True)

    state = get_state()
    for session_id in list(state["sessions"]):
        sess = state["sessions"].pop(session_id, None)
        if not sess:
            continue
        try:
            sess["capture"] = None  # release a waiting capture
            _finalize_log(sess)
        except Exception:
            logger.debug(
                "Log finalize failed for %s.", session_id, exc_info=True
            )
        try:
            sess["conn"].disconnect()
        except Exception:
            logger.debug(
                "Disconnect failed for %s.", session_id, exc_info=True
            )

    logger.info("Shutdown complete.")


atexit.register(shutdown)
