"""Terminus — local, desktop-style web SSH terminal.

Application factory + runtime configuration. Creates the ~/terminus
working directory and a persistent encryption key on import, then exposes
``create_app()`` for the web and desktop launchers.

File path: terminus/app.py
"""
import logging
import os
import sys
import secrets

from flask import Flask
from flask_socketio import SocketIO

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Runtime configuration (constants first, so sibling modules can import them
# without triggering a circular import via create_app()).
# ---------------------------------------------------------------------------
BASE_DIR = os.path.expanduser("~/.terminus")
LOG_DIR = os.path.join(BASE_DIR, "logs")
DB_PATH = os.path.join(BASE_DIR, "terminus.db")
_KEY_PATH = os.path.join(BASE_DIR, ".key")

HOST = "127.0.0.1"
PORT = 5001

if sys.platform == "win32":
    LOCAL_SHELL = os.environ.get("COMSPEC", "cmd.exe")
    LOCAL_SHELL_LABEL = os.path.basename(LOCAL_SHELL)
else:
    LOCAL_SHELL = os.environ.get("SHELL", "/bin/bash")
    LOCAL_SHELL_LABEL = os.path.basename(LOCAL_SHELL)


def _load_or_create_secret():
    """Load the on-disk encryption key, generating one on first run."""
    os.makedirs(BASE_DIR, exist_ok=True)
    if os.path.exists(_KEY_PATH):
        with open(_KEY_PATH, "r", encoding="utf-8") as fh:
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
from .routes import register_routes
from .sockets import register_socket_handlers


def create_app():
    """Build and return ``(app, socketio)`` in threading mode."""
    app = Flask(__name__)
    app.secret_key = SECRET_KEY

    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

    register_routes(app)
    register_socket_handlers(socketio)
    return app, socketio
