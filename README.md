# Terminus

A local, desktop-style **web SSH terminal** for network devices. Terminus runs
a small Flask + Socket.IO server on your machine and gives you a modern,
multi-session terminal — in a browser tab or a native desktop window — with
per-session logging, encrypted connector storage, live command broadcast, local
shell access, and a themeable UI.

> Single-user, localhost-only by design. Terminus is **not** meant to be hosted
> on a shared or public server.

---

## Features

- **Multi-session tabs** — open many device sessions at once; switch, close, or
  reconnect them from a floating sidebar.
- **Connectors** — save reusable credential profiles (device + optional jump
  host). Passwords are **encrypted at rest**.
- **Test connection** — verify a connector against a host before using it.
- **Local shell** — open a native shell (PowerShell on Windows, your `$SHELL`
  on Linux/macOS) right alongside your SSH sessions.
- **Command broadcast** — type once, send to every active SSH session.
- **Session logging** — every session (SSH or local shell) is teed to a clean,
  ANSI-stripped `.log` file you can view, download, or delete in-app.
- **Appearance** — light/dark themes on a gradient canvas, plus a selectable
  terminal font (Google Sans Code bundled; JetBrains Mono, Fira Code, IBM Plex
  Mono, Source Code Pro from Google Fonts).
- **Three ways to run** — a development launcher, a browser launcher, and a
  native desktop-window launcher.

---

## Project structure

```
Terminus/
├── terminus/
│   ├── __init__.py
│   ├── app.py                # runtime config + application factory (create_app)
│   ├── routes.py             # HTTP views + route registration
│   ├── sockets.py            # Socket.IO handlers, session lifecycle, state
│   ├── services.py           # stateless SSH connection helpers (netcore)
│   ├── shell.py        # local PTY adapter (pywinpty / stdlib pty)
│   ├── credentials.py        # SQLite connector store + Fernet encryption
│   ├── templates/
│   │   └── terminus.html
│   └── static/
│       ├── css/common.css
│       ├── js/{core,sessions,settings}.js
│       ├── fonts/
│       └── img/
├── dev_launcher.py           # development (no caching, reloader, debug)
├── web_launcher.py           # run in a browser
├── desktop_launcher.py       # run as a native window (pywebview)
├── requirements.txt
├── README.md
└── LICENSE
```

### Where your data lives

Terminus stores everything under **`~/.terminus`** (your home directory), never
inside the app folder:

| Path                      | Purpose                                    |
|---------------------------| ------------------------------------------ |
| `~/.terminus/terminus.db` | SQLite store of connectors (encrypted)     |
| `~/.terminus/logs/`       | Per-session `.log` files                   |
| `~/.terminus/.key`        | Persistent encryption key (auto-generated) |
| `~/.terminus/webview/`    | Desktop window storage (theme, cache)      |
| `~/.terminus/prefs/`      | User preferences (theme, font, etc.) |

The `.key` file is created on first run and reused thereafter, so encrypted
connector passwords remain readable across restarts. **Back it up** if you back
up the database — losing the key makes stored passwords unrecoverable (you'd
just re-enter them).

---

## Requirements

- **Python 3.11+**
- The internal **`netcore`** SSH library (provides `GenericHandler`)
- Packages in `requirements.txt`:
  - `Flask`, `Flask-SocketIO`, `cryptography`
  - `pywebview` (desktop launcher only)
  - `pywinpty` (**Windows only** — local shell support; installed
    automatically via a platform marker)

---

## Setup

```bash
# 1. Clone / copy the project, then create a virtual environment
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt
```

Make sure `netcore` is importable in the same environment (installed via pip or
on your `PYTHONPATH`).

---

## Running

Always run from the **project root** (the folder containing `terminus/`) so the
package imports correctly.

### Development

```bash
python dev_launcher.py
```

Runs the browser server with **caching fully disabled** (no-cache headers,
template auto-reload) and the Werkzeug reloader on. Use this while iterating on
HTML/CSS/JS so edits show on every reload.

### Browser

```bash
python web_launcher.py
```

Starts the server on `http://127.0.0.1:5001` and opens your default browser.

### Desktop window

```bash
python desktop_launcher.py
```

Opens Terminus in a native, maximized window with the custom title-bar/taskbar
icon. Uses a free port automatically if 5001 is busy.

For a clean, console-less launch (Windows), use `pythonw`:

```bash
pythonw desktop_launcher.py
```

---

## Desktop shortcut (Windows)

1. Right-click the desktop → **New → Shortcut**.
2. **Target:**

   ```
   "C:\path\to\venv\Scripts\pythonw.exe" "C:\path\to\Terminus\desktop_launcher.py"
   ```

3. Open the shortcut's **Properties** and set:
   - **Start in:** `C:\path\to\Terminus`
   - **Change Icon…:** `C:\path\to\Terminus\terminus\static\img\terminus.ico`
4. Name it **Terminus**. Double-click to launch.

---

## Using Terminus

### 1. Add a connector

Open **Settings → Connectors → Add**:

- **Connector name** — a label (e.g. `core-lab`).
- **Device credentials** — username / password for the target devices.
- **Jump host** *(optional)* — bastion IP + credentials, if required.
- **Test connection** — enter a host and click *Test connection* to verify.

> When editing a connector, leaving a password field **blank keeps the existing
> stored password** — passwords are never sent back to the browser.

### 2. Open a session

Click **New Session**, choose a connector, and enter one or more
hostnames/IPs (one per line). Each opens as its own terminal.

To open a **local shell** instead, click **Local Shell** at the bottom-left of
the New Session dialog. It spawns your machine's shell (PowerShell on Windows,
`$SHELL` on Unix) as a fully interactive terminal.

### 3. Work

- **Copy** — select text (auto-copies). **Paste** — right-click.
- **Broadcast** — type a command in the sidebar's *Send to all active…* box
  (sent to active SSH sessions).
- **Scroll** — mouse wheel or the terminal scrollbar (buffered scrollback).
- **Download / close** — hover a session row, or use the terminal header
  buttons for the active session.

### 4. Manage logs

**Settings → Files** lists every session log (SSH and local shell). Filter,
view inline, download, or delete. Logs tied to an active session are protected
from deletion.

### 5. Appearance

**Settings → Appearance** — pick a theme (Light / Dark) and a terminal font.
Choices persist across restarts (stored in the browser / webview
`localStorage`).

---

## Configuration

Runtime settings live at the top of `terminus/app.py`:

| Setting             | Default               | Description                              |
| ------------------- |-----------------------| ---------------------------------------- |
| `HOST`              | `127.0.0.1`           | Bind address (keep localhost)            |
| `PORT`              | `5001`                | Preferred port                           |
| `BASE_DIR`          | `~/.terminus`         | Root for DB, logs, key, and webview data |
| `LOCAL_SHELL`       | `$COMSPEC` / `$SHELL` | Command used for the Local Shell feature |
| `LOCAL_SHELL_LABEL` | shell basename        | Label shown for local shell sessions     |

On Windows, set `LOCAL_SHELL` to `cmd.exe` or `pwsh.exe` if you prefer. The
desktop launcher automatically falls back to a free port if `PORT` is in use.

---

## Architecture notes

- **App factory** — `terminus.app.create_app()` returns `(app, socketio)` in
  **threading** mode (no eventlet/gevent, no monkey-patching). All launchers
  call it.
- **Unified channel interface** — SSH sessions and local shells share the same
  socket read/input/resize/logging pipeline. `shell.py` wraps the
  platform PTY (pywinpty on Windows, stdlib `pty` on POSIX) to expose the same
  channel interface the SSH layer uses.
- **State** — active sessions and log metadata live in a process-wide dict in
  `sockets.py` (single process, single user — no external store needed).
- **Namespace** — all Socket.IO traffic is under `/terminus`.
- **Security posture** — no authentication by design; Terminus binds to
  localhost and runs like a personal desktop tool. The Local Shell feature can
  run arbitrary local commands — acceptable for a single-user localhost app.
  Connector passwords are encrypted with Fernet, keyed off `~/.terminus/.key`.

---

## Troubleshooting

**"Address already in use" (web/dev launcher).**
Something is already on port 5001. Stop it, or change `PORT` in
`terminus/app.py`. (The desktop launcher auto-picks a free port.)

**HTML/CSS/JS edits don't show.**
Use `dev_launcher.py` while developing — it disables all caching. In the
browser, a hard reload (Ctrl+F5) also busts the cache. The desktop window
caches assets in `~/.terminus/webview`.

**Desktop window shows the Python icon.**
Ensure `terminus/static/img/terminus.ico` exists. When run via `python` the icon is
applied at runtime; give it a second or two after the window appears.

**Local Shell fails to open (Windows).**
Install `pywinpty` (`pip install pywinpty`, or reinstall from
`requirements.txt`). On Unix no extra package is needed — the stdlib `pty`
module is used.

**Terminal font doesn't change.**
Confirm the font is available. Bundled: *Google Sans Code*. The others load from
Google Fonts (needs internet). In the browser console:
`document.fonts.check('400 13px "JetBrains Mono"')` should return `true`.

**Stored passwords stopped working.**
The `~/.terminus/.key` file was likely regenerated or moved. Restore the original
key, or re-enter the affected connector passwords.

---

## License

See [LICENSE](LICENSE).