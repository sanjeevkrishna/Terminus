# Terminus

A local, desktop-style **web SSH terminal** for network engineers. Terminus runs
a small Flask + Socket.IO server on your own machine and gives you a modern,
multi-session terminal — in a browser tab or a native desktop window — with
per-session logging, encrypted credential storage, command broadcast, local
shell access, and an optional AI Assistant that can propose read-only
diagnostic commands for you to approve.

> **Single-user, localhost-only by design.** Terminus has no authentication and
> can spawn local shells. It is a personal desktop tool and must **not** be
> hosted on a shared or public server.

---

## Features

- **Multi-session terminals** — open many devices at once and switch between
  them from a floating sidebar. Scrollback is preserved per session.
- **Connectors** — reusable credential profiles (device plus optional jump
  host), with passwords **encrypted at rest** and never sent back to the
  browser.
- **Test connection** — open and close a real SSH session to verify a connector
  before you rely on it.
- **Platform auto-detection** — the device platform is resolved cheapest-first:
  an explicit setting, then login-banner fingerprints, then prompt shape, then
  Netmiko's probes. Paging and terminal width are normalised on connect.
- **Local shells** — PowerShell, cmd, WSL, bash, zsh and others, in the same
  interface as your SSH sessions.
- **Broadcast** — type once, send to every selected session.
- **Session logging** — every session is teed to a clean `.log` file with cursor
  movements and escape sequences resolved, so the file reads the way the screen
  looked. View, open or delete them in-app.
- **AI Assistant** *(optional, off by default)* — ask questions about your live
  sessions. The model reads recent terminal output and can propose **read-only**
  commands; nothing runs until you approve it, and every command is
  policy-checked server-side. See [AI Assistant](#ai-assistant).
- **Themes and fonts** — six themes on a gradient canvas, adjustable terminal
  font size, and a performance mode for VDI or GPU-less machines.
- **Works offline** — all JavaScript, CSS, the UI font, the icon font and the
  default terminal font are vendored. No telemetry, and no calls out except to
  devices you connect to and an AI provider you configure. Optional terminal
  fonts load from Google Fonts when available and fall back cleanly when not.
- **Three launch modes** — native desktop window, browser tab, or development
  mode with the reloader.

---

## Requirements

- **Python 3.11+** (CI tests 3.11 and 3.12; installers are built on 3.12)
- A terminal-capable SSH target, or just a local shell.

Install:

```bash
python -m venv venv

# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -e ".[desktop]"
```

`pyproject.toml` is the single source of truth for dependencies. Installing
editable also makes `terminus` importable from anywhere, which is what lets
`pytest` run without any `PYTHONPATH` setup.

Optional extras, only if you want the Assistant:

```bash
pip install -e ".[desktop,azure]"     # Azure OpenAI
pip install -e ".[desktop,ollama]"    # Ollama, local or self-hosted
```

Development tools (pytest, ruff):

```bash
pip install -e ".[desktop,dev]"
```

### Vendored assets

Everything the app needs to run ships with the repository — no runtime CDN
dependencies for JavaScript, and the app works fully offline:

| Path | Contents |
|---|---|
| `terminus/static/js/vendor/` | xterm, xterm-addon-fit, socket.io, marked, DOMPurify |
| `terminus/static/css/vendor/` | xterm stylesheet |
| `terminus/static/fonts/` | Google Sans Code (default terminal face), Inter (UI), Material Symbols Rounded (icons) |

Additional terminal fonts offered in Settings load from Google Fonts and need
internet. Offline they fall back to the bundled Google Sans Code; nothing
breaks, and the stylesheet is loaded non-blocking so an offline start is not
delayed by it.

If a JavaScript file is missing, Terminus shows an error page naming it rather
than failing silently. Keep the socket.io client version matched to the
`Flask-SocketIO` server or the handshake will fail.

---

## Running

Always run from the **project root** — the folder containing `terminus/`. There
is one entry point:

```bash
python run.py              # native desktop window (default)
python run.py browser      # open in your default browser
python run.py dev          # browser, caching off, reloader on
```

| Flag | Effect |
|---|---|
| `--port 5055` | Bind a specific port |
| `--log DEBUG` | Verbose logging (overrides `TERMINUS_LOG`) |
| `--no-browser` | Browser and dev modes: start the server without opening a tab |
| `--selftest` | Wire the app, print `OK selftest`, exit 0. Used by CI to validate frozen builds |

Serves on `http://127.0.0.1:5001` by default; desktop mode picks a free port if
that one is taken.

For a console-less launch on Windows: `pythonw run.py`.

`run.py` must stay in the project root — both `launchers` and `terminus` are
resolved relative to the running script's directory.

### Logging

All modes honour `TERMINUS_LOG`, or `--log`:

```bash
# Windows PowerShell
$env:TERMINUS_LOG="DEBUG"; python run.py

# macOS / Linux
TERMINUS_LOG=DEBUG python run.py

# or, any platform
python run.py --log DEBUG
```

Logs go to stderr and to `~/.terminus/terminus-app.log` (rotating, 2 MB × 3).
The file matters most in desktop mode, which has no console — it is the first
place to look when something misbehaves.

---

## Using Terminus

### Add a connector

**Settings → Connectors → Add.** Give it a name, device credentials, and a jump
host if you need one. **Test connection** against a real host to confirm it
works.

Editing a connector leaves password fields blank — submitting blank **keeps the
stored password**. Passwords are never sent to the browser.

Setting **Device type** explicitly skips auto-detection, which is faster and
more reliable when you already know the platform.

### Open sessions

**New Session**, pick a connector, and enter one hostname or IP per line — each
opens its own terminal. Or use the **Local Shell** split-button to spawn a shell
on your own machine.

### Work

| Action | How |
|---|---|
| Copy | Select text — it copies automatically |
| Paste | Right-click |
| Broadcast | **Tools → Broadcast**, select sessions, <kbd>Ctrl</kbd>+<kbd>Enter</kbd> |
| Ask the Assistant | **Tools → Assistant**, or the ✨ button in the terminal header |
| Open a session's log | The 🗗 button on the session row or in the header |
| Close a modal | <kbd>Esc</kbd> — except when a terminal has focus, where Esc goes to the device |

### Manage logs

**Logs** in the top bar. Filter, view inline, open with your OS's default
application, or delete. Logs belonging to a live session are protected from
deletion.

### Appearance

**Settings → Appearance** — pick a theme, terminal font and size.
**Performance mode** disables blur and translucency and gives xterm an opaque
background, which is worth enabling on VDI, remote desktop, or machines without
a GPU.

Preferences are stored server-side in `~/.terminus/prefs.json`, so they follow
you between the browser and the desktop window.

---

## AI Assistant

Optional and **off by default**. When enabled, the Assistant reads recent output
from the sessions you select and can request commands to run. Leaving it
disabled means no provider is ever initialised and nothing leaves your machine.

### How a turn works

```text
you ask a question
  → the model reads the selected sessions' recent output
  → it proposes commands, per device
  → Terminus policy-checks every one and refuses anything unsafe
  → YOU APPROVE, EDIT OR DENY
  → approved commands run in your own terminal, visibly
  → output goes back to the model, which answers
```

Up to five rounds per question, then it must answer with what it has.

### What is sent

Recent session output and the output of commands you approved. Passwords, keys,
community strings, SNMP strings and certificates are **masked before anything
leaves the machine**. Hostnames, IP addresses, interface names, configuration
and command output are **sent as-is**.

Choose a provider your organisation permits. An Ollama instance on your own
hardware keeps everything on your infrastructure.

### What it may run

Only **read-only** diagnostic commands. `terminus/ai/policy.py` classifies every
proposed command into four tiers and this build executes only the first:

| Tier | Examples | Executed |
|---|---|---|
| `read_only` | `show`, `display`, `dir`, `ls`, `df`, bounded `ping` | ✅ with your approval |
| `mutating` | `configure terminal`, `commit`, `write memory` | ⛔ refused |
| `destructive` | `no …`, `shutdown`, `clear ip bgp`, `delete` | ⛔ refused |
| `forbidden` | `reload`, `erase`, `format`, `rm -rf`, `zeroize` | ⛔ never, in any build |

Structural rules apply on top: one command per entry, no embedded newlines, no
shell chaining or redirection, pipes restricted to filters, and
credential-dumping patterns refused even when the base verb is `show`. Commands
you edit in the approval card are **re-validated server-side** — the browser is
never the security boundary.

### Guarantees

- Nothing runs without an explicit click.
- Every AI-issued command appears in your terminal, marked `── AI ▶ …`, and is
  written to the session log with a header and footer. The log stays a complete
  record of everything that touched the device.
- Your keystrokes are blocked on a session only while a command is running on
  it, so input can never interleave with captured output.
- A session that is busy, in configuration mode, or not at a prompt is skipped
  rather than disturbed.

### Setup

**Settings → AI.** Pick a provider, fill in the connection fields, **Test**,
then enable the feature and accept the disclaimer.

**Azure OpenAI** — needs a GPT-4-class deployment. Smaller models propose wrong
commands and mishandle tool calls.

**Ollama** — the Assistant is off by default. Tick *Enable the interactive
Assistant* and use a tool-calling family (`qwen2.5`, `qwen3`, `llama3.3`,
`mistral-nemo`, `command-r`, `hermes3`, `granite3`) at roughly 24B parameters or
more. The settings page tells you live whether your model qualifies. Smaller
models are a frustrating experience rather than a dangerous one — policy and
approval apply regardless.

Model output can be wrong. You remain responsible for what runs on your
infrastructure.

---

## Project structure

```text
Terminus/
├── run.py                      # entry point — python run.py [desktop|browser|dev]
├── pyproject.toml              # dependencies, packaging, pytest, ruff
├── launchers/
│   ├── common.py               # logging, port resolution, readiness
│   ├── browser.py              # browser tab
│   ├── dev.py                  # browser, no caching, reloader
│   └── desktop.py              # native pywebview window
├── terminus/
│   ├── app.py                  # runtime config, create_app(), graceful shutdown
│   ├── paths.py                # resource resolution for source and frozen builds
│   ├── crypto.py               # Fernet helpers for values stored at rest
│   ├── credentials.py          # SQLite connector store
│   ├── services.py             # SSH: connect, jump host, platform detection
│   ├── shell.py                # local PTY adapter (pywinpty / stdlib pty)
│   ├── transcript.py           # session log (disk), transcript (memory), monitor
│   ├── logbanner.py            # shared log-file header
│   ├── routes.py               # HTTP views + origin guard
│   ├── sockets.py              # Socket.IO handlers, session lifecycle, state
│   ├── ai/                     # optional subpackage — imported defensively
│   │   ├── providers.py        # Azure / Ollama, streaming events, redaction
│   │   ├── settings.py         # AI settings store
│   │   ├── policy.py           # command risk classification — the safety boundary
│   │   ├── executor.py         # runs approved commands against live channels
│   │   └── agent.py            # tool-calling loop with human approval
│   ├── templates/
│   │   └── terminus.html
│   └── static/
│       ├── css/                # common.css + vendored xterm stylesheet
│       ├── js/
│       │   ├── core.js         # TW namespace, appearance, modals, markdown
│       │   ├── sessions.js     # terminal lifecycle, sidebar, socket events
│       │   ├── settings.js     # New Session, Settings, Logs modals
│       │   ├── tools.js        # Tools modal, shared selection, Broadcast
│       │   ├── ai.chat.js      # Assistant conversation and approval cards
│       │   ├── ai.settings.js  # AI provider form
│       │   └── vendor/         # xterm, socket.io, marked, DOMPurify
│       ├── fonts/
│       └── img/                # terminus.png / .ico / .icns (committed)
├── build/
│   ├── terminus.spec           # PyInstaller spec
│   ├── terminus.iss            # Inno Setup installer script
│   └── rthook_streams.py       # runtime hook: stdout/stderr guard
├── tests/                      # pytest suite
├── .github/workflows/
│   ├── ci.yml                  # tests + lint on push and PR
│   └── release.yml             # installers for all platforms on tag push
└── LICENSE
```

`launchers/` sits outside the package on purpose: those are entry points rather
than library code. The consequence is that `pip install .` does not ship them —
Terminus is run from its source directory or from an installer.

`build/` holds only committed **inputs**. PyInstaller's working tree goes to
`.pyi-cache/` and its output to `dist/`, both gitignored.

### Where your data lives

Everything lives under **`~/.terminus`**, never inside the app folder:

| Path | Purpose |
|---|---|
| `~/.terminus/terminus.db` | Connectors and AI settings (secrets encrypted) |
| `~/.terminus/logs/` | Per-session `.log` files |
| `~/.terminus/prefs.json` | Theme, font, performance mode |
| `~/.terminus/.key` | Encryption key, generated on first run |
| `~/.terminus/terminus-app.log` | Application log |
| `~/.terminus/webview/` | Desktop window storage and cache |

**Back up `.key` if you back up the database.** Without it, stored passwords are
unrecoverable — you would re-enter them.

---

## Architecture notes

- **App factory** — `create_app(port)` returns `(app, socketio)` in **threading**
  mode. No eventlet, no gevent, no monkey-patching. The port is a parameter
  because the origin allowlist is derived from it.
- **One channel interface** — SSH sessions and local shells expose the same
  `recv_ready` / `recv` / `send` / `resize_pty` / `closed` surface, so the socket
  layer treats them identically.
- **One reader per channel** — the socket read loop is the only code that calls
  `recv()`. It fans bytes to the client, the session log, an idle monitor, and
  an executor capture when the Assistant is running a command. This is why
  AI-issued commands are visible in your terminal as they run.
- **Adaptive session logging** — interactive output goes through a headless
  `pyte` terminal so the log matches what the screen showed; flood output
  switches to a fast raw-append path and back again when it goes quiet.
- **Optional AI** — `terminus/ai/` is an isolated subpackage. Core modules never
  import it; `routes.py` imports it inside a `try/except ImportError` and
  degrades to `available: false`, so an import failure disables the feature
  instead of breaking the app.
- **Security posture** — no authentication, by design. Bound to `127.0.0.1`,
  with a Socket.IO origin allowlist, a `Host`/`Origin` guard on HTTP routes, and
  a per-launch token the page embeds and the socket handshake requires. Those
  three together stop a website you happen to have open from driving your
  terminal. Connector passwords and AI secrets are Fernet-encrypted at rest.
- **Graceful shutdown** — closing the window cancels in-flight AI turns, flushes
  the buffered session logs, writes each log's footer, and closes the SSH
  connections.

---

## Development

```bash
pip install -e ".[desktop,dev,azure,ollama]"
pytest -q
```

The AI extras are needed because the test suite imports `terminus.ai.executor`.

The suite covers command policy, completion detection, the executor against a
scripted fake channel, transcript and log behaviour, redaction, and provider
event normalisation. It runs in seconds and needs no device.

```bash
ruff check .          # lint
ruff format .         # format
```

`.github/workflows/ci.yml` runs the same checks on Linux, Windows and macOS
against Python 3.11 and 3.12 for every push to `main` and every pull request.

---

## Building executables

Standalone builds need no Python on the target machine. Releases are produced by
CI on tag push; the steps below are for building locally.

### Setup

Use a dedicated virtualenv — PyInstaller bundles whatever it finds, so an
environment shared with other projects produces a larger, non-reproducible
build:

```bash
python -m venv .venv-build
# Windows
.\.venv-build\Scripts\activate
# macOS / Linux
source .venv-build/bin/activate

pip install -e ".[desktop,azure,ollama]"
pip install pyinstaller
```

### Build

`build/terminus.spec` reads three environment variables:

| Variable | Default | Effect |
|---|---|---|
| `TERMINUS_VERSION` | `0.0.0` | Stamped into the Windows version resource and macOS `Info.plist` |
| `TERMINUS_GUI` | `1` | `1` windowed, `0` keeps a console |
| `TERMINUS_WITH_AI` | `1` | `0` skips collecting the provider SDKs |

```powershell
# Windows
$env:TERMINUS_VERSION = "1.2.0"
$env:TERMINUS_GUI     = "1"

pyinstaller build\terminus.spec --noconfirm `
  --distpath dist\windowed --workpath .pyi-cache\windowed
```

```bash
# macOS / Linux
export TERMINUS_VERSION=1.2.0
export TERMINUS_GUI=1

pyinstaller build/terminus.spec --noconfirm \
  --distpath dist/windowed --workpath .pyi-cache/windowed
```

Output is a **directory**, not a single file. One-file mode extracts the whole
bundle to a temporary directory on every launch, costing several seconds of
startup for no real benefit once an installer is involved.

Build with `TERMINUS_GUI=0` first and run it from a terminal: a console build
shows import errors that a windowed build swallows.

```powershell
dist\windowed\Terminus\Terminus.exe --selftest
```

> **`TERMINUS_WITH_AI=0` does not produce an AI-free bundle.** `routes.py`
> imports `terminus.ai` inside a `try/except`, and PyInstaller's static
> analysis follows that import regardless of the flag. The variable only skips
> `collect_submodules("openai")`. To run without AI, leave the feature disabled
> in **Settings → AI** — no provider is initialised and nothing leaves the
> machine.

### What the spec handles

Three things static analysis cannot see, each of which fails in a way that is
hard to diagnose:

| Item | Symptom if missing |
|---|---|
| `engineio.async_drivers.threading` | Server starts, then refuses every socket connection |
| netmiko's driver tree | Platform auto-detection fails on connect |
| pywinpty's `OpenConsole.exe` and `winpty-agent.exe` | Local shells die at spawn — `CreatePseudoConsole` appears to succeed, then every read raises `Pty is closed`. These are spawned **by path**, not imported |

A windowed build also has no console, so `sys.stdout`/`stderr` are `None`.
`build/rthook_streams.py` substitutes `os.devnull` before any bundled module
runs; without it a library writing to stdout raises `AttributeError` inside a
background thread and the feature simply stalls.

Provider dependencies are worth noting: `openai` 3.x depends on **`httpx2`**,
not `httpx`, and `httpx` arrives only with the `ollama` extra. The OAuth token
request in `providers.py` therefore uses `urllib.request` from the standard
library, so it cannot break when either extra is absent.

### Verify a build

Work through these in order — each catches a distinct bundling failure:

1. Page loads and the terminal grid renders — bundled templates and static files
2. Icons are glyphs, not words — bundled fonts
3. Socket connects (devtools → Network → **WS**) — the engineio driver
4. A local shell reaches a prompt — pywinpty helper executables
5. SSH to a device **without** an explicit device type — netmiko's driver tree
6. **Settings → AI → Test** passes — the provider SDK and its transport
7. Ask the Assistant and approve a command — the whole agent path
8. Close the app, then check `~/.terminus/logs/` for the `Session Ended` footer

### Windows installer

Requires [Inno Setup 6](https://jrsoftware.org/isdl.php).

```powershell
$iscc = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"

& $iscc `
  "/DMyAppVersion=1.2.0" `
  "/DMyAppVersionNumeric=1.2.0" `
  "/DDistDir=$((Resolve-Path dist\windowed\Terminus).Path)" `
  build\terminus.iss
```

`MyAppVersion` is the display string and may carry a suffix such as
`1.2.0-rc1`. `MyAppVersionNumeric` feeds `VersionInfoVersion`, which accepts
digits and dots only.

Produces `dist/installer/Terminus-<version>-win64-setup.exe`. The installer:

- installs **per-user** by default, so no UAC prompt
- asks how Terminus should launch — **desktop window** or **browser** — and
  writes that choice into the shortcut parameters
- warns, without blocking, if the Edge WebView2 runtime is absent
- closes a running Terminus via Restart Manager before an upgrade, so locked
  `_internal` files do not fail the install
- asks before removing `~/.terminus` on uninstall — that directory holds saved
  connectors and the encryption key

Silent installs can pick the launch mode too:

```powershell
Terminus-1.2.0-win64-setup.exe /SILENT /launchmode=browser
```

Syntax-check the script without a full build by pointing `DistDir` at any folder
containing a file named `Terminus.exe`:

```powershell
New-Item -ItemType Directory -Force "$env:TEMP\isstub" | Out-Null
New-Item -ItemType File -Force "$env:TEMP\isstub\Terminus.exe" | Out-Null
& $iscc "/DMyAppVersion=0.0.0" "/DMyAppVersionNumeric=0.0.0" `
        "/DDistDir=$env:TEMP\isstub" build\terminus.iss
```

### macOS

The spec emits a `.app` bundle on macOS. Wrap it in a DMG:

```bash
mkdir -p dist/installer dmg-staging
cp -R dist/windowed/Terminus.app dmg-staging/
ln -s /Applications dmg-staging/Applications

hdiutil create -volname "Terminus 1.2.0" -srcfolder dmg-staging \
    -ov -format UDZO "dist/installer/Terminus-1.2.0-macos-arm64.dmg"
rm -rf dmg-staging
```

CI builds **arm64 only**. Intel `macos-13` runners are being retired, and a
`universal2` build is not viable while netmiko's dependency tree includes
single-architecture wheels.

An unsigned `.app` is blocked by Gatekeeper. Users must right-click → **Open**.
CI applies an ad-hoc signature (`codesign --sign -`), which is enough to launch
but is not a substitute for a Developer ID. With one:

```bash
codesign --deep --force --options runtime \
    --sign "Developer ID Application: Your Org (TEAMID)" dist/windowed/Terminus.app
xcrun notarytool submit dist/installer/Terminus-*.dmg \
    --apple-id you@example.com --team-id TEAMID --wait
xcrun stapler staple dist/installer/Terminus-*.dmg
```

### Linux

CI produces an x86_64 AppImage, assembling the `AppDir`, `.desktop` entry and
`AppRun` script inline — see `.github/workflows/release.yml`. Browser mode
always works; the desktop window depends on GTK and WebKit2 being present on the
host, which PyInstaller does not bundle reliably.

Build on the oldest distribution you intend to support: glibc is
forward-compatible, not backward. CI uses `ubuntu-22.04` for this reason.

For Linux users, `pipx install git+<repo>` is often a better experience than an
AppImage — smaller, and it gives the native desktop window too.

### Signing and antivirus

Unsigned PyInstaller binaries that bundle paramiko draw antivirus false
positives fairly regularly. UPX compression makes this markedly worse, so the
spec disables it. Test against corporate endpoint protection before distributing
widely, and expect to submit at least one false-positive report.

Windows SmartScreen warns on every unsigned installer until the executable
accumulates reputation. Signing with an EV certificate avoids this entirely.

### Releasing

```bash
git tag -a v1.2.0 -m "Release 1.2.0"
git push --follow-tags
```

`.github/workflows/release.yml` runs the tests, builds Windows, macOS arm64 and
Linux, smoke-tests each binary with `--selftest`, and opens a **draft** release
with SHA-256 checksums. Download the artifacts and check one before publishing —
a CI build uses a different Python patch level and wheel set than your local
one.

The workflow also runs from the Actions tab (**Run workflow**), which builds the
artifacts without creating a release. Useful for testing the pipeline itself.

---

## Troubleshooting

**"Address already in use."**
Something else holds port 5001. Use `--port`, or let desktop mode pick a free
port automatically.

**Terminal is laggy, or the Assistant streams in bursts.**
`simple-websocket` is probably missing, so Socket.IO fell back to HTTP
long-polling. Reinstall with `pip install -e ".[desktop]"`, then check
devtools → Network → WS shows one `websocket` connection rather than repeated
`?transport=polling`.

**"Terminus could not start" with a list of files.**
Vendored JavaScript is missing from `terminus/static/js/vendor/`. See
[Vendored assets](#vendored-assets).

**Icons render as words like `add` or `close`.**
`terminus/static/fonts/MaterialSymbolsRounded.woff2` failed to load. Check the
file exists and the Network tab shows it fetched.

**A terminal font won't apply.**
Only *Google Sans Code* is bundled; the others load from Google Fonts and need
internet. Check the Network tab for a failed `fonts.googleapis.com` request.

**Local shell won't open on Windows.**
`pip install pywinpty`. No extra package is needed on POSIX.

**The Assistant says the provider cannot run it.**
The provider does not have tool calling enabled. For Ollama, tick *Enable the
interactive Assistant* and use a large tool-calling model.

**A command comes back `busy` or `wrong_mode`.**
Working as intended — the session was producing output or sitting in
configuration mode. Terminus refuses to interfere. Return to exec mode, or wait.

**Stored passwords stopped working.**
`~/.terminus/.key` was regenerated or moved. Restore it, or re-enter the
affected passwords.

**The AI settings page shows the feature as unavailable.**
`terminus/ai/` failed to import — usually a missing provider SDK. Install
`pip install -e ".[desktop,azure]"` or check `~/.terminus/terminus-app.log` for
the `ImportError`.

**Every request returns 403, or the socket will not connect (dev mode).**
The reloader binds the port in the parent process and re-executes the child; if
the child resolves a *different* port it builds its origin allowlist for the
wrong one. `launchers/common.resolve_port()` exports the resolved port through
the environment to prevent this. If you see it, check both startup lines report
the same port:

```text
INFO terminus.app: App configured for 127.0.0.1:5001 (origin allowlist active).
INFO launchers.dev: Terminus DEV on http://127.0.0.1:5001 …
```

**"This page is out of date — Terminus restarted."**
The launch token is regenerated on every start, so a browser tab left open
across a restart can never reconnect. Reload the page. A matching
`Refused socket … stale token` line appears in the server log.

**A frozen build starts but local shells fail immediately.**
pywinpty's helper executables are missing. They are spawned by path rather than
imported, so PyInstaller cannot detect them; `build/terminus.spec` collects them
explicitly. Check:

```powershell
Get-ChildItem dist\windowed\Terminus\_internal\winpty | Select-Object Name
```

You need `OpenConsole.exe`, `winpty-agent.exe`, `winpty.dll` and `conpty.dll`.

**A frozen build stalls with no error.**
A windowed build has no console, so writing to `sys.stdout` raises
`AttributeError` — which becomes a silent stall inside a background thread.
`build/rthook_streams.py` prevents this. Check `~/.terminus/terminus-app.log`,
and reproduce with `TERMINUS_GUI=0` to see the traceback.

**`ModuleNotFoundError` for a provider dependency in a frozen build.**
A lazy `import` inside a function is invisible to PyInstaller unless the package
is installed in the build environment *and* listed in the spec's
`hiddenimports`. Build with the AI extras installed:
`pip install -e ".[desktop,azure,ollama]"`.

---

## License

See [LICENSE](LICENSE).