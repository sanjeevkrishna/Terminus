"""Terminus — a local, desktop-style web SSH terminal.

A single-user Flask + Socket.IO application providing an in-browser or
native-window SSH terminal with per-session logging, encrypted connector
storage, and an optional AI Assistant.

Package layout:
    app.py          Runtime config, application factory, graceful shutdown
    crypto.py       Fernet helpers for values stored at rest
    credentials.py  SQLite connector store
    services.py     Stateless SSH connection helpers
    shell.py        Local shell PTY adapter
    transcript.py   Session log (disk), transcript (memory), idle monitor
    logbanner.py    Shared log-file header
    routes.py       HTTP views + route registration
    sockets.py      Socket.IO handlers, session lifecycle, shared state
    ai/             Optional: providers, policy, executor, agent

File path: terminus/__init__.py
"""

__version__ = "1.2.0"
__all__ = ["__version__"]
