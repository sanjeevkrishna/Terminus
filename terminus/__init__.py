"""Terminus — a local, desktop-style web SSH terminal.

A single-user Flask + Socket.IO application that provides an in-browser
(or native-window) SSH terminal with per-session logging, encrypted
connector storage, and a themed UI.

Package layout:
    app.py          Runtime config + application factory (create_app)
    routes.py       HTTP views + route registration
    sockets.py      Socket.IO handlers, SSH lifecycle, shared session state
    services.py     Stateless SSH connection helpers (netcore)
    credentials.py  SQLite connector store with encrypted secrets

File path: terminus/__init__.py
"""

__version__ = "1.0.0"
__all__ = ["__version__"]