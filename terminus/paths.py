"""Resource path resolution, for both source and frozen builds.

PyInstaller unpacks bundled data to ``sys._MEIPASS``, so anything read by path
rather than by import needs to go through here.

File path: terminus/paths.py
"""

import os
import sys

FROZEN = getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def resource_root():
    """Directory containing the bundled ``terminus/`` tree.

    Frozen: the PyInstaller extraction directory. Source: the project root.
    """
    if FROZEN:
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def package_dir():
    """The ``terminus`` package directory, wherever it actually lives."""
    return os.path.join(resource_root(), "terminus")


def resource(*parts):
    """Absolute path to a bundled resource under ``terminus/``."""
    return os.path.join(package_dir(), *parts)
