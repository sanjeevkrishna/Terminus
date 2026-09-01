"""Terminus launchers.

Each module exposes ``main(port=None, log_level=None, open_browser=True)`` and is
selected by ``run.py`` in the project root.

These live outside the ``terminus`` package on purpose: they are entry points
rather than library code, and keeping them here leaves the package itself free of
launcher concerns. The consequence is that they are not installed by
``pip install .`` — Terminus is run from its source directory.

File path: launchers/__init__.py
"""

__all__ = ["browser", "desktop", "dev"]
