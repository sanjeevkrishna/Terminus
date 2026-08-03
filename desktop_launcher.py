"""Terminus — desktop launcher.

Starts the Flask + Socket.IO server on a background thread and opens a
native pywebview window pointing at it. Applies a custom title-bar/taskbar
icon and tints the native title bar to match the app theme (Windows).
Closing the window exits the app.

File path: desktop_launcher.py
"""
import ctypes
import json
import logging
import os
import socket
import sys
import threading
import time

import webview  # pywebview

from terminus.app import create_app, HOST, PORT

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

WINDOW_TITLE = "Terminus"
BASE_DIR = os.path.expanduser("~/.terminus")
PREFS_PATH = os.path.join(BASE_DIR, "prefs.json")
ICON_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "terminus", "static", "img", "terminus.ico",
)

# Title-bar caption colors (COLORREF = 0x00BBGGRR). Match your theme bg.
CAPTION_DARK = 0x001E1E1E   # #1e1e1e
CAPTION_LIGHT = 0x00FAF8F6  # #f6f8fa (BGR of #f6f8fa)

app, socketio = create_app()

# Distinct taskbar identity so Windows does not group us under python.exe.
if sys.platform == "win32":
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Terminus.App")


# ---------------------------------------------------------------------------
# Server helpers
# ---------------------------------------------------------------------------
def _resolve_port(preferred):
    """Return the preferred port if free, otherwise an OS-assigned one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((HOST, preferred))
            return preferred
        except OSError:
            sock.bind((HOST, 0))
            return sock.getsockname()[1]


def _run_server(port):
    # allow_unsafe_werkzeug is fine for a local, single-user desktop app.
    socketio.run(app, host=HOST, port=port,
                 allow_unsafe_werkzeug=True, use_reloader=False)


def _wait_until_up(port, timeout=8.0):
    """Block until the server accepts connections, or until *timeout*."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex((HOST, port)) == 0:
                return True
        time.sleep(0.1)
    logger.warning("Server did not come up within %.1fs.", timeout)
    return False


# ---------------------------------------------------------------------------
# Native window helpers (Windows)
# ---------------------------------------------------------------------------
def _find_window(timeout=5.0):
    """Return the HWND of our window, waiting until it exists."""
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            return hwnd
        time.sleep(0.1)
    return None


def _apply_window_icon(hwnd=None):
    """Push the custom .ico onto the native window (Windows only)."""
    if sys.platform != "win32":
        return
    if not os.path.exists(ICON_PATH):
        logger.warning("Icon not found: %s", ICON_PATH)
        return

    hwnd = hwnd or _find_window()
    if not hwnd:
        logger.warning("Could not find window to apply icon.")
        return

    user32 = ctypes.windll.user32
    WM_SETICON = 0x0080
    ICON_SMALL, ICON_BIG = 0, 1
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010

    def _load(size):
        return user32.LoadImageW(
            None, ICON_PATH, IMAGE_ICON, size, size, LR_LOADFROMFILE
        )

    hicon_small = _load(16)
    hicon_big = _load(32)
    if hicon_small:
        user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
    if hicon_big:
        user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)


def _apply_titlebar_theme(dark, hwnd=None):
    """Tint the native title bar to match the app theme (Windows 10/11)."""
    if sys.platform != "win32":
        return

    hwnd = hwnd or _find_window()
    if not hwnd:
        return

    dwmapi = ctypes.windll.dwmapi
    DWMWA_USE_IMMERSIVE_DARK_MODE = 20  # 19 on older Win10 builds
    DWMWA_CAPTION_COLOR = 35            # Windows 11 only

    # 1) Dark/light title bar toggle (try attr 20, then legacy 19).
    flag = ctypes.c_int(1 if dark else 0)
    for attr in (DWMWA_USE_IMMERSIVE_DARK_MODE, 19):
        try:
            dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(flag), ctypes.sizeof(flag)
            )
        except Exception:
            pass

    # 2) (Windows 11) explicit caption color to match the palette.
    color = ctypes.c_int(CAPTION_DARK if dark else CAPTION_LIGHT)
    try:
        dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_CAPTION_COLOR, ctypes.byref(color), ctypes.sizeof(color)
        )
    except Exception:
        pass  # silently ignored on Windows 10


def _current_theme():
    """Read the persisted theme from ~/.terminus/prefs.json."""
    try:
        with open(PREFS_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh).get("theme", "dark")
    except (OSError, ValueError):
        return "dark"


# ---------------------------------------------------------------------------
# JS -> Python bridge (live title-bar theming)
# ---------------------------------------------------------------------------
class Bridge:
    """Exposed to the page as ``window.pywebview.api``."""

    def set_titlebar_theme(self, theme):
        """Called from core.js when the user switches theme in-app."""
        _apply_titlebar_theme(theme == "dark")
        return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    port = _resolve_port(PORT)

    server = threading.Thread(target=_run_server, args=(port,), daemon=True)
    server.start()
    _wait_until_up(port)

    webview.create_window(
        title=WINDOW_TITLE,
        url=f"http://{HOST}:{port}",
        maximized=True,
        confirm_close=True,
        js_api=Bridge(),
    )

    def _post_window():
        # Both need the native window handle; find it once.
        hwnd = _find_window()
        _apply_window_icon(hwnd)
        _apply_titlebar_theme(_current_theme() == "dark", hwnd)

    threading.Thread(target=_post_window, daemon=True).start()

    storage_dir = os.path.join(BASE_DIR, "webview")
    os.makedirs(storage_dir, exist_ok=True)
    webview.start(private_mode=False, storage_path=storage_dir)


if __name__ == "__main__":
    main()