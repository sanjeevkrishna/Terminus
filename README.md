# Terminus

A local, desktop-style **web SSH terminal** for network devices. Terminus runs
a small Flask + Socket.IO server on your machine and gives you a modern,
multi-session terminal — in a browser tab or a native desktop window — with
per-session logging, encrypted connector storage, live command broadcast, and a
themeable UI.

> Single-user, localhost-only by design. Terminus is **not** meant to be hosted
> on a shared or public server.

---

## Features

- **Multi-session tabs** — open many device sessions at once; switch, close, or
  reconnect them from a floating sidebar.
- **Connectors** — save reusable credential profiles (device + optional jump
  host). Passwords are **encrypted at rest**.
- **Test connection** — verify a connector against a host before using it.
- **Command broadcast** — type once, send to every active session.
- **Session logging** — every session is teed to a clean, ANSI-stripped `.log`
  file you can view, download, or delete in-app.
- **Appearance** — light/dark themes on a gradient canvas, plus a selectable
  terminal font (Google Sans Code bundled; JetBrains Mono, Fira Code, IBM Plex
  Mono, Source Code Pro from Google Fonts).
- **Two ways to run** — a browser launcher and a native desktop-window launcher.

---

## Project structure

```
Terminus/
├── terminus/                 # application package
│   ├── __init__.py           # package metadata (__version__)
│   ├── app.py                # runtime config + application factory (create_app)
│   ├── routes.py             # HTTP views + route registration
│   ├── sockets.py            # Socket.IO handlers, SSH lifecycle, session state
│   ├── services.py           # stateless SSH connection helpers (netcore)
│   ├── credentials.py        # SQLite connector store + Fernet encryption
│   ├── templates/
│   │   └── terminus.html
│   └── static/
│       ├── css/common.css
│       ├── js/{core,sessions,settings}.js
│       ├── fonts/            # GoogleSansCode.woff2, MaterialSymbolsRounded.woff2
│       └── img/              # brand.ico, brand.png
├── web_launcher.py           # run in a browser
├── desktop_launcher.py       # run as a native window (pywebview)
├── requirements.txt
├── README.md
└── LICENSE
```

### Where your data lives

Terminus stores everything under **`~/terminus`** (your home directory), never
inside the app folder:

| Path                         | Purpose                                   |
| ---------------------------- | ----------------------------------------- |
| `~/terminus/terminus.db`     | SQLite store of connectors (encrypted)    |
| `~/terminus/logs/`           | Per-session `.log` files                  |
| `~/terminus/.key`            | Persistent encryption key (auto-generated)|

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
  - `pywebview` (only needed for the desktop launcher)

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

---

## Running

Always run from the **project root** (the folder containing `terminus/`) so the
package imports correctly.

### Browser

```bash
python web_launcher.py
```

Starts the server on `http://127.0.0.1:5001` and opens your default browser.
Code changes auto-reload (Werkzeug reloader enabled).

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
   - **Change Icon…:** `C:\path\to\Terminus\terminus\static\img\brand.ico`
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

### 3. Work

- **Copy** — select text (auto-copies). **Paste** — right-click.
- **Broadcast** — type a command in the sidebar's *Send to all active…* box.
- **Download / close** — hover a session row, or use the terminal header
  buttons for the active session.

### 4. Manage logs

**Settings → Files** lists every session log. Filter, view inline, download, or
delete. Logs tied to an active session are protected from deletion.

### 5. Appearance

**Settings → Appearance** — pick a theme (Light / Dark) and a terminal font.
Choices persist across restarts (stored in the browser's `localStorage`).

---

## Configuration

Runtime settings live at the top of `terminus/app.py`:

| Setting     | Default          | Description                          |
| ----------- | ---------------- | ------------------------------------ |
| `HOST`      | `127.0.0.1`      | Bind address (keep localhost)        |
| `PORT`      | `5001`           | Preferred port                       |
| `BASE_DIR`  | `~/terminus`     | Root for DB, logs, and key           |

The desktop launcher automatically falls back to a free port if `PORT` is in
use.

---

## Architecture notes

- **App factory** — `terminus.app.create_app()` returns `(app, socketio)` in
  **threading** mode (no eventlet/gevent, no monkey-patching). Both launchers
  call it.
- **State** — active sessions and log metadata live in a process-wide dict in
  `sockets.py` (single process, single user — no external store needed).
- **Namespace** — all Socket.IO traffic is under `/terminus`.
- **Security posture** — no authentication by design; Terminus binds to
  localhost and is intended to run like a personal desktop tool. Connector
  passwords are encrypted with Fernet, keyed off `~/terminus/.key`.

---

## Troubleshooting

**"Address already in use" (web launcher).**
Something is already on port 5001. Stop it, or change `PORT` in
`terminus/app.py`. (The desktop launcher auto-picks a free port.)

**Desktop window shows the Python icon.**
Ensure `terminus/static/img/brand.ico` exists. When run via `python` the icon is
applied at runtime; give it a second or two after the window appears.

**Terminal font doesn't change.**
Confirm the font is available. Bundled: *Google Sans Code*. The others load from
Google Fonts (needs internet). In the browser console:
`document.fonts.check('400 13px "JetBrains Mono"')` should return `true`.

**Stored passwords stopped working.**
The `~/terminus/.key` file was likely regenerated or moved. Restore the original
key, or re-enter the affected connector passwords.

---

## License

See [LICENSE](LICENSE).