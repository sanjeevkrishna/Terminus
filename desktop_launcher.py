"""Terminus — desktop launcher.

Starts the Flask + Socket.IO server on a background thread and opens a
native pywebview window pointing at it. Applies a custom title-bar /
taskbar icon (Windows). Closing the window exits the app.

File path: desktop_launcher.py
"""
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
ICON_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "terminus", "static", "img", "terminus.ico",
)

app, socketio = create_app()

# Distinct taskbar identity so Windows does not group us under python.exe.
if sys.platform == "win32":
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Terminus.App")


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


def _apply_window_icon():
    """Push the custom .ico onto the native window (Windows only)."""
    if sys.platform != "win32":
        return
    if not os.path.exists(ICON_PATH):
        logger.warning("Icon not found: %s", ICON_PATH)
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

    for _ in range(50):  # retry up to ~5s while the window is created
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            if hicon_small:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
            if hicon_big:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
            return
        time.sleep(0.1)
    logger.warning("Could not find window to apply icon.")


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
    )

    threading.Thread(target=_apply_window_icon, daemon=True).start()

    storage_dir = os.path.join(os.path.expanduser("~/terminus"), "webview")
    os.makedirs(storage_dir, exist_ok=True)
    webview.start(private_mode=False, storage_path=storage_dir)

if __name__ == "__main__":
    main()