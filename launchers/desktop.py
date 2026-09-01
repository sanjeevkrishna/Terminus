"""Desktop launcher — native pywebview window over a background server.

Applies a custom title-bar/taskbar icon, tints the native title bar to match the
app theme (Windows), and runs a graceful shutdown on close so buffered session
logs are flushed and any AI turn awaiting approval is abandoned cleanly.

File path: launchers/desktop.py
"""

import atexit
import ctypes
import json
import logging
import os
import sys
import threading
import time

import webview  # pywebview
from terminus.app import BASE_DIR, HOST, PREFS_PATH, create_app, shutdown
from terminus.paths import resource

from . import common

logger = logging.getLogger(__name__)

WINDOW_TITLE = "Terminus"
WINDOW_MIN_SIZE = (1024, 640)  # below this the sidebar crushes the terminal
SHUTDOWN_TIMEOUT = 20.0  # a disconnect can block on the network
ICON_PATH = resource("static", "img", "terminus.ico")

# Title-bar caption colours per theme (COLORREF = 0x00BBGGRR).
_DARK_THEMES = {"dark", "nord", "dracula", "gruvbox"}
_CAPTION_COLORS = {
    "light": 0x00FAF8F6,  # #f6f8fa
    "solarized-light": 0x00E3F6FD,  # #fdf6e3
    "dark": 0x001E1E1E,  # #1e1e1e
    "nord": 0x0040342E,  # #2e3440
    "dracula": 0x00362A28,  # #282a36
    "gruvbox": 0x00282828,  # #282828
}

# Resolved in main(); the Bridge and post-window hooks reuse them rather than
# re-running a window search on every call.
_hwnd = None
_window = None
app = None
socketio = None


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
def _run_server(port):
    try:
        # allow_unsafe_werkzeug is fine for a local, single-user desktop app.
        socketio.run(
            app,
            host=HOST,
            port=port,
            allow_unsafe_werkzeug=True,
            use_reloader=False,
        )
    except Exception:
        logger.exception("Server thread died.")


def _start_server(preferred=None, attempts=3):
    """Resolve a port, build the app for it, and start serving.

    The app is created per attempt because the origin allowlist and Host check
    are derived from the bound port.
    """
    global app, socketio

    port = preferred
    for attempt in range(1, attempts + 1):
        port = (
            common.pick_port(preferred) if attempt == 1 else common.free_port()
        )
        app, socketio = create_app(port)
        logger.info(
            "Starting server on %s:%s (attempt %d).", HOST, port, attempt
        )
        threading.Thread(
            target=_run_server,
            args=(port,),
            name="terminus-server",
            daemon=True,
        ).start()
        if common.wait_until_up(port):
            logger.info("Server is up on %s:%s.", HOST, port)
            return port
        logger.warning("Server did not come up on port %s.", port)
    return None


# ---------------------------------------------------------------------------
# Native window helpers (Windows)
# ---------------------------------------------------------------------------
def _to_int_handle(handle):
    """Coerce a .NET IntPtr, ctypes handle or int to a plain int."""
    if handle is None:
        return 0
    for attr in ("ToInt64", "ToInt32"):
        method = getattr(handle, attr, None)
        if callable(method):
            try:
                return int(method())
            except Exception:
                pass
    try:
        return int(handle)
    except (TypeError, ValueError):
        return 0


def _top_level(hwnd):
    """Return the top-level ancestor of *hwnd*.

    pywebview's ``window.native`` is often the embedded browser control, whose
    HWND is a child. WM_SETICON and the DWM caption attributes only apply to
    top-level windows, so they fail silently on a child handle.
    """
    if not hwnd or sys.platform != "win32":
        return hwnd
    GA_ROOT = 2
    try:
        root = ctypes.windll.user32.GetAncestor(hwnd, GA_ROOT)
        return int(root) if root else hwnd
    except Exception:
        return hwnd


def _native_hwnd(window, timeout=8.0):
    """Resolve the top-level HWND for our window.

    Prefers the handle pywebview owns (normalised to its root) because
    ``FindWindowW`` matches on title alone and would happily return a second
    Terminus instance's window.
    """
    if sys.platform != "win32":
        return None

    user32 = ctypes.windll.user32
    deadline = time.time() + timeout

    while time.time() < deadline:
        for source in ("native", "gui"):
            candidate = getattr(window, source, None)
            hwnd = _to_int_handle(getattr(candidate, "Handle", None))
            if hwnd:
                root = _top_level(hwnd)
                if user32.IsWindow(root):
                    logger.info(
                        "Window handle via %s: child=%s root=%s",
                        source,
                        hwnd,
                        root,
                    )
                    return root

        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            logger.info("Window handle via FindWindowW: %s", hwnd)
            return int(hwnd)

        time.sleep(0.1)

    logger.warning("Could not resolve a native window handle.")
    return None


def _apply_window_icon(hwnd):
    """Push the custom .ico onto the native window (Windows only)."""
    if sys.platform != "win32" or not hwnd:
        return
    if not os.path.exists(ICON_PATH):
        logger.warning("Icon not found: %s", ICON_PATH)
        return

    user32 = ctypes.windll.user32
    WM_SETICON = 0x0080
    ICON_SMALL, ICON_BIG = 0, 1
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010
    LR_DEFAULTSIZE = 0x00000040

    def _load(size):
        flags = LR_LOADFROMFILE | (LR_DEFAULTSIZE if not size else 0)
        return user32.LoadImageW(
            None, ICON_PATH, IMAGE_ICON, size, size, flags
        )

    try:
        small = _load(user32.GetSystemMetrics(49) or 16)  # SM_CXSMICON
        big = _load(user32.GetSystemMetrics(11) or 32)  # SM_CXICON
        if not small and not big:
            logger.warning(
                "LoadImageW returned no icon for %s (err=%s).",
                ICON_PATH,
                ctypes.get_last_error(),
            )
            return
        if small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
        if big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
        logger.info(
            "Window icon applied to hwnd=%s (small=%s big=%s).",
            hwnd,
            bool(small),
            bool(big),
        )
    except Exception:
        logger.exception("Could not apply the window icon.")


def _apply_titlebar_theme(theme, hwnd=None):
    """Tint the native title bar to match the app theme (Windows 10/11)."""
    if sys.platform != "win32":
        return
    hwnd = hwnd or _hwnd
    if not hwnd:
        return

    dark = theme in _DARK_THEMES
    dwmapi = ctypes.windll.dwmapi
    DWMWA_USE_IMMERSIVE_DARK_MODE = 20  # 19 on older Win10 builds
    DWMWA_CAPTION_COLOR = 35  # Windows 11 only

    flag = ctypes.c_int(1 if dark else 0)
    for attr in (DWMWA_USE_IMMERSIVE_DARK_MODE, 19):
        try:
            dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(flag), ctypes.sizeof(flag)
            )
        except Exception:
            pass

    caption = _CAPTION_COLORS.get(theme, 0x001E1E1E if dark else 0x00FAF8F6)
    color = ctypes.c_int(caption)
    try:
        dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_CAPTION_COLOR,
            ctypes.byref(color),
            ctypes.sizeof(color),
        )
    except Exception:
        pass  # silently ignored on Windows 10


def _current_theme():
    """Read the persisted theme from ~/.terminus/prefs.json."""
    try:
        with open(PREFS_PATH, encoding="utf-8") as fh:
            return json.load(fh).get("theme", "dark")
    except (OSError, ValueError):
        return "dark"


# ---------------------------------------------------------------------------
# JS -> Python bridge (live title-bar theming)
# ---------------------------------------------------------------------------
class Bridge:
    """Exposed to the page as ``window.pywebview.api``."""

    def set_titlebar_theme(self, theme):
        """Called from core.js when the user switches theme in-app.

        Uses the cached handle: re-running the window search here would block
        the bridge thread on every theme change.
        """
        _apply_titlebar_theme(str(theme or ""), _hwnd)
        return True


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------
def _graceful_shutdown():
    """Run the app's teardown, bounded so a hung disconnect cannot wedge exit."""
    worker = threading.Thread(
        target=shutdown, name="terminus-shutdown", daemon=True
    )
    worker.start()
    worker.join(timeout=SHUTDOWN_TIMEOUT)
    if worker.is_alive():
        logger.warning(
            "Shutdown did not finish within %.0fs — exiting anyway.",
            SHUTDOWN_TIMEOUT,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(port=None, log_level=None, open_browser=True):
    common.configure_logging(log_level)
    logger.info("Terminus starting (log: %s).", common.APP_LOG_PATH)

    # Distinct taskbar identity so Windows does not group us under python.exe.
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Terminus.App"
            )
        except Exception:
            logger.debug("Could not set the AppUserModelID.", exc_info=True)

    resolved = _start_server(port)
    if resolved is None:
        logger.error("Could not start the Terminus server. Aborting.")
        if sys.stderr is not None:
            print(
                "Terminus could not start its local server — see "
                f"{common.APP_LOG_PATH}",
                file=sys.stderr,
            )
        return 1

    global _window
    _window = webview.create_window(
        title=WINDOW_TITLE,
        url=f"http://{HOST}:{resolved}",
        maximized=True,
        min_size=WINDOW_MIN_SIZE,
        # Native confirmation only — sessions.js suppresses its own
        # beforeunload prompt when window.pywebview is present, so the user is
        # asked exactly once.
        confirm_close=True,
        js_api=Bridge(),
    )

    def _post_window():
        global _hwnd
        _hwnd = _native_hwnd(_window)
        if not _hwnd:
            logger.warning(
                "No native handle — skipping icon and title-bar theming."
            )
            return
        theme = _current_theme()
        _apply_window_icon(_hwnd)
        _apply_titlebar_theme(theme, _hwnd)

        # WebView2 finishes initialising after the window is shown and can
        # clobber both; reassert once.
        time.sleep(1.5)
        _apply_window_icon(_hwnd)
        _apply_titlebar_theme(theme, _hwnd)

    threading.Thread(
        target=_post_window, name="terminus-window", daemon=True
    ).start()

    # atexit covers a signal or hard interpreter exit; the explicit call after
    # start() covers the normal window-close path.
    atexit.register(_graceful_shutdown)

    storage_dir = os.path.join(BASE_DIR, "webview")
    os.makedirs(storage_dir, exist_ok=True)

    try:
        webview.start(
            private_mode=False, storage_path=storage_dir, icon=ICON_PATH
        )
    except TypeError:
        # Older pywebview builds have no icon= parameter.
        webview.start(private_mode=False, storage_path=storage_dir)
    finally:
        logger.info("Window closed — running shutdown.")
        _graceful_shutdown()
    return 0
