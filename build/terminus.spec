# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Terminus.

Environment variables read by this file:

    TERMINUS_WITH_AI    "1" (default) to bundle the AI provider SDKs
    TERMINUS_GUI        "1" (default) for a windowed build (no console)
    TERMINUS_VERSION    version string stamped into the macOS Info.plist

Invoked directly by CI:

    pyinstaller build/terminus.spec --noconfirm \
        --distpath dist/<variant> --workpath build/work-<variant>

Icons are committed binaries, not generated at build time:

    terminus/static/img/terminus.png     master artwork (Linux uses it directly)
    terminus/static/img/terminus.ico     Windows
    terminus/static/img/terminus.icns    macOS

File path: build/terminus.spec
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# ---------------------------------------------------------------------------
# Paths and build flags
# ---------------------------------------------------------------------------
SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent

APP_NAME = "Terminus"
VERSION = os.environ.get("TERMINUS_VERSION", "0.0.0")
WITH_AI = os.environ.get("TERMINUS_WITH_AI", "1") == "1"
GUI = os.environ.get("TERMINUS_GUI", "1") == "1"

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"

ENTRY = ROOT / "run.py"
RTHOOK = ROOT / "build" / "rthook_streams.py"

if not ENTRY.is_file():
    raise SystemExit(f"[spec] entry point not found: {ENTRY}")

# The runtime hook redirects sys.stdout/stderr to os.devnull when they are None.
# Without it, a windowed build stalls the moment anything writes to stdout.
if not RTHOOK.is_file():
    raise SystemExit(
        f"[spec] runtime hook not found: {RTHOOK}\n"
        f"        It is required - a windowed build hangs without it."
    )

print(f"[spec] {APP_NAME} {VERSION} - {'windowed' if GUI else 'console'}, "
      f"AI {'bundled' if WITH_AI else 'excluded'}")

# ---------------------------------------------------------------------------
# Icon
#
# Committed per-platform assets. Linux embeds nothing in the binary; the
# AppImage picks up the PNG separately.
# ---------------------------------------------------------------------------
ICON_DIR = ROOT / "terminus" / "static" / "img"
_ICON_NAME = {"win32": "terminus.ico", "darwin": "terminus.icns"}.get(sys.platform)

ICON = None
if _ICON_NAME:
    _icon_path = ICON_DIR / _ICON_NAME
    if not _icon_path.is_file():
        raise SystemExit(
            f"[spec] {_ICON_NAME} not found in {ICON_DIR}\n"
            f"        Icons are committed assets. Regenerate with "
            f"tools/make_icns.py (.icns) or Pillow (.ico) and commit."
        )
    ICON = str(_icon_path)
    print(f"[spec] icon: {ICON}")

# ---------------------------------------------------------------------------
# Windows VERSIONINFO resource
#
# Without this, Explorer shows blank version fields and some AV heuristics
# treat the binary as more suspicious. Requires a 4-part numeric tuple, so
# strip any pre-release suffix from TERMINUS_VERSION.
# ---------------------------------------------------------------------------
VERSION_INFO = None
if WINDOWS:
    import re

    _digits = re.match(r"(\d+(?:\.\d+)*)", VERSION)
    _parts = [int(p) for p in _digits.group(1).split(".")] if _digits else [0]
    _parts = (_parts + [0, 0, 0, 0])[:4]
    _tuple = tuple(_parts)

    try:
        from PyInstaller.utils.win32.versioninfo import (
            FixedFileInfo, StringFileInfo, StringStruct, StringTable,
            VarFileInfo, VarStruct, VSVersionInfo,
        )

        VERSION_INFO = VSVersionInfo(
            ffi=FixedFileInfo(filevers=_tuple, prodvers=_tuple),
            kids=[
                StringFileInfo([
                    StringTable("040904B0", [
                        StringStruct("CompanyName", "Cisco Systems, Inc."),
                        StringStruct("FileDescription", "Terminus SSH client with AI assistant"),
                        StringStruct("FileVersion", VERSION),
                        StringStruct("InternalName", APP_NAME),
                        StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                        StringStruct("ProductName", APP_NAME),
                        StringStruct("ProductVersion", VERSION),
                    ]),
                ]),
                VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
            ],
        )
        print(f"[spec] version resource: {'.'.join(map(str, _tuple))} ({VERSION})")
    except Exception as exc:
        print(f"[spec] WARNING could not build version resource: {exc}")

# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------
datas = [
    (str(ROOT / "terminus" / "templates"), "terminus/templates"),
    (str(ROOT / "terminus" / "static"), "terminus/static"),
]

binaries = []

# ---------------------------------------------------------------------------
# Hidden imports - runtime-resolved imports that static analysis misses
# ---------------------------------------------------------------------------
hiddenimports = [
    # engineio selects its async driver by string name at runtime. Without
    # this the server starts, then refuses every socket connection.
    "engineio.async_drivers.threading",
    # Resolved by engineio only when a WebSocket upgrade is attempted.
    "simple_websocket",
]

# netmiko builds device drivers from SSH_MAPPER_BASE by name.
hiddenimports += collect_submodules("netmiko")

if WITH_AI:
    try:
        import openai  # noqa: F401
    except ImportError:
        raise SystemExit(
            "[spec] TERMINUS_WITH_AI=1 but openai is not installed.\n"
            "        collect_submodules would silently bundle nothing.\n"
            "        Run: pip install -e \".[desktop,azure,ollama]\""
        ) from None

    def _skip_voice_helpers(name):
        """openai.helpers pulls in numpy for audio, which Terminus never uses."""
        return not name.startswith("openai.helpers")

    hiddenimports += collect_submodules("openai", filter=_skip_voice_helpers)
    hiddenimports += [
        "httpx2", "httpcore2", "truststore", "anyio", "idna", "certifi",
        "httpx", "httpcore", "h11", "jiter",
    ]
    datas += collect_data_files("openai")

    try:
        hiddenimports += collect_submodules("ollama")
    except Exception:
        print("[spec] ollama not installed - that provider stays unusable")

if GUI and WINDOWS:
    # pywebview ships its own hook (webview/__pyinstaller) which selects the
    # correct platform backend; collecting its submodules here would also drag
    # in the GTK and Qt backends. The EdgeChromium backend needs pythonnet.
    try:
        import clr  # noqa: F401

        hiddenimports += ["clr", "clr_loader", "pythonnet"]
    except ImportError:
        print("[spec] pythonnet not installed - EdgeChromium backend may fail")

# ---------------------------------------------------------------------------
# pywinpty helper binaries
#
# Not discoverable by static analysis. Missing OpenConsole.exe manifests as
# EOFError the first time a user opens a local shell.
# ---------------------------------------------------------------------------
if WINDOWS:
    try:
        import winpty

        _winpty_dir = Path(winpty.__file__).parent
        _collected = []
        for _path in _winpty_dir.rglob("*"):
            if not _path.is_file():
                continue
            if _path.suffix.lower() in (".py", ".pyc", ".pyi"):
                continue
            if "tests" in _path.parts or "__pycache__" in _path.parts:
                continue

            _dest = Path("winpty") / _path.parent.relative_to(_winpty_dir)
            if _path.suffix.lower() in (".dll", ".pyd"):
                binaries.append((str(_path), str(_dest)))
            else:
                # Helper executables go in datas: as binaries, PyInstaller
                # would run dependency analysis on them and may relocate them.
                datas.append((str(_path), str(_dest)))
            _collected.append(_path.name)

        print(f"[spec] pywinpty: {sorted(_collected)}")
        if not any(n.lower() == "openconsole.exe" for n in _collected):
            print("[spec] WARNING OpenConsole.exe not found - local shells may fail")
    except ImportError:
        print("[spec] pywinpty not installed - local shells unavailable")

# ---------------------------------------------------------------------------
# Exclusions - netmiko and cryptography drag in a great deal that is unused
# ---------------------------------------------------------------------------
excludes = [
    # GUI toolkits Terminus does not use
    "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",

    # Scientific stack, in case it is present in the build environment
    "matplotlib", "numpy", "pandas", "scipy", "PIL",

    # Notebook tooling
    "IPython", "jupyter", "notebook", "ipykernel",

    # Alternative Socket.IO async backends - we use threading
    "eventlet", "gevent", "geventwebsocket",

    # Test frameworks
    "pytest", "_pytest",
]

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
analysis = Analysis(
    [str(ENTRY)],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(RTHOOK)],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX dramatically increases AV false positives
    console=not GUI,
    disable_windowed_traceback=False,
    argv_emulation=False,    # Terminus opens no documents; emulation can hang
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)

collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

if MACOS:
    app = BUNDLE(
        collect,
        name=f"{APP_NAME}.app",
        icon=ICON,
        bundle_identifier="com.cisco.terminus",
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            # pywebview drives a WKWebView. Without this, App Transport
            # Security blocks the http://127.0.0.1 origin Flask serves on and
            # the window comes up blank.
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        },
    )