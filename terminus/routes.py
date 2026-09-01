"""HTTP views + route registration.

Serves the terminal page, manages session log files (list / view /
download / delete), and provides connector CRUD plus a test-connection
endpoint. Routes are registered directly on the Flask app (no blueprint).

File path: terminus/routes.py
"""

import glob
import json
import logging
import os
import subprocess
import sys
import time
from urllib.parse import urlparse

from flask import (
    abort,
    current_app,
    jsonify,
    render_template,
    request,
    send_file,
)

from .app import LOCAL_SHELLS, LOG_DIR, PREFS_PATH
from .credentials import get_store
from .services import connector_to_params, test_connection
from .sockets import get_state

try:
    from .ai import (
        PROVIDER_SCHEMA,
        get_ai_store,
        provider_capabilities,
        public_schema,
        test_provider,
    )

    AI_AVAILABLE = True
except ImportError:
    PROVIDER_SCHEMA = {}
    get_ai_store = None
    provider_capabilities = None
    public_schema = None
    test_provider = None
    AI_AVAILABLE = False

logger = logging.getLogger(__name__)

_CONNECTOR_FIELDS = (
    "network_username",
    "network_password",
    "jumphost_ip",
    "jumphost_username",
    "jumphost_password",
    "device_type",
    "ssh_options",
)


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------
def _log_dir():
    os.makedirs(LOG_DIR, exist_ok=True)
    return LOG_DIR


def _safe_resolve(log_dir, filename):
    """Resolve *filename* inside *log_dir*, guarding against traversal."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        abort(400, description="Invalid filename.")
    full = os.path.realpath(os.path.join(log_dir, filename))
    root = os.path.realpath(log_dir) + os.sep
    if not full.startswith(root):
        abort(403, description="Invalid path.")
    if not os.path.isfile(full):
        abort(404, description="File not found.")
    return full


def _flush_session_log(sess):
    """Push the emulated screen to disk so an opened log is up to date."""
    session_log = (sess or {}).get("session_log")
    if session_log:
        session_log.snapshot()


def _flush_if_live(path):
    """Flush a log that belongs to a live session.

    Writes are buffered for throughput and recent output may still sit inside
    the terminal emulator, so a log opened by filename would look truncated
    without this.
    """
    target = os.path.realpath(path)
    for sess in get_state()["sessions"].values():
        logpath = sess.get("logpath")
        if logpath and os.path.realpath(logpath) == target:
            _flush_session_log(sess)
            return True
    return False


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
def render():
    prefs = _read_prefs()
    return render_template(
        "terminus.html",
        theme=prefs.get("theme", "light"),
        launch_token=current_app.config.get("TERMINUS_TOKEN", ""),
    )


# ---------------------------------------------------------------------------
# Open in the OS (desktop app — server and user are the same machine)
# ---------------------------------------------------------------------------
def _open_with_os(path):
    """Open *path* with the OS-associated application / file manager."""
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True, ""
    except Exception as exc:
        logger.warning("Could not open %s: %s", path, exc)
        return False, str(exc)


def open_log(filename):
    """Open a log file with its associated application."""
    full = _safe_resolve(_log_dir(), filename)
    _flush_if_live(full)
    ok, message = _open_with_os(full)
    return jsonify({"ok": ok, "message": message})


def open_log_folder():
    """Open the log directory in the system file manager."""
    ok, message = _open_with_os(_log_dir())
    return jsonify({"ok": ok, "message": message})


def open_session_log(session_id):
    """Open the log of a live (or recently ended) session."""
    state = get_state()
    entry = state["logs"].get(session_id)
    if not entry:
        if session_id in state["sessions"]:
            abort(
                409,
                description="Log not ready yet — session still initializing.",
            )
        abort(404, description="No log for this session.")

    real_log = os.path.realpath(entry["path"])
    sess = state["sessions"].get(session_id)
    log_dir = (sess or {}).get("log_dir") or _log_dir()
    if not real_log.startswith(os.path.realpath(log_dir) + os.sep):
        abort(403, description="Invalid path.")
    if not os.path.isfile(real_log):
        abort(404, description="Log file not found on disk.")

    if sess:
        _flush_session_log(sess)  # get the newest content onto disk first

    ok, message = _open_with_os(real_log)
    return jsonify({"ok": ok, "message": message})


def list_shells():
    """Return the local shells available on this machine."""
    return jsonify(
        {
            "shells": [
                {"id": s["id"], "label": s["label"]} for s in LOCAL_SHELLS
            ]
        }
    )


# ---------------------------------------------------------------------------
# Preferences (theme / font) — persisted server-side, launcher-independent
# ---------------------------------------------------------------------------
def _read_prefs():
    try:
        with open(PREFS_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def get_prefs():
    return jsonify(_read_prefs())


def save_prefs():
    body = request.get_json(silent=True) or {}
    prefs = _read_prefs()
    for key in ("theme", "font", "font_size", "shell", "perf_mode"):
        if key in body:
            prefs[key] = body[key]
    try:
        with open(PREFS_PATH, "w", encoding="utf-8") as fh:
            json.dump(prefs, fh)
    except OSError:
        abort(500, description="Could not save preferences.")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Logs: list / view / delete
# ---------------------------------------------------------------------------
def list_logs():
    log_dir = _log_dir()

    # Flush live sessions so the reported sizes are current.
    for sess in get_state()["sessions"].values():
        _flush_session_log(sess)

    files = []
    for path in glob.glob(os.path.join(log_dir, "*.log")):
        try:
            st = os.stat(path)
        except OSError:
            continue
        files.append(
            {
                "filename": os.path.basename(path),
                "created": int(st.st_ctime),
                "created_str": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(st.st_ctime)
                ),
                "size": st.st_size,
            }
        )
    files.sort(key=lambda f: f["created"], reverse=True)
    return jsonify({"logs": files})


def view_log(filename):
    full = _safe_resolve(_log_dir(), filename)
    _flush_if_live(full)
    return send_file(full, mimetype="text/plain", as_attachment=False)


def delete_logs():
    log_dir = _log_dir()
    payload = request.get_json(silent=True) or {}
    filenames = payload.get("filenames")
    if not isinstance(filenames, list) or not filenames:
        abort(400, description="Expected a non-empty 'filenames' list.")

    active_paths = {
        os.path.realpath(s["logpath"])
        for s in get_state()["sessions"].values()
        if s.get("logpath")
    }

    deleted, skipped, errors = [], [], []
    for name in filenames:
        try:
            full = _safe_resolve(log_dir, name)
        except Exception:
            errors.append(name)
            continue
        if os.path.realpath(full) in active_paths:
            skipped.append(name)
            continue
        try:
            os.remove(full)
            deleted.append(name)
        except OSError:
            errors.append(name)

    return jsonify(
        {
            "ok": True,
            "deleted": deleted,
            "skipped": skipped,
            "errors": errors,
        }
    )


# ---------------------------------------------------------------------------
# Connector CRUD + test
# ---------------------------------------------------------------------------
def list_connectors():
    store = get_store()
    connectors = {
        name: {"jumphost": store.has_jumphost(name)}
        for name in store.list_names()
    }
    return jsonify({"connectors": connectors})


def get_connector(name):
    connector = get_store().get(name)
    if not connector:
        abort(404)
    # Never expose stored passwords to the browser.
    connector.pop("network_password", None)
    connector.pop("jumphost_password", None)
    connector["name"] = name
    return jsonify(connector)


def save_connector():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        abort(400, description="Name required.")
    connector = {field: body.get(field, "") for field in _CONNECTOR_FIELDS}
    get_store().upsert(name, connector)
    return jsonify({"ok": True})


def delete_connector(name):
    return jsonify({"ok": get_store().delete(name)})


def test_connector():
    """Test an SSH connection.

    Accepts an existing connector name (+ hostname) or full inline fields.
    For a saved connector, blank password fields fall back to stored values.
    """
    body = request.get_json(silent=True) or {}
    hostname = (body.get("hostname") or "").strip()
    if not hostname:
        abort(400, description="Hostname required for a connection test.")

    name = (body.get("name") or "").strip()
    stored = get_store().get(name) if name else None

    connector = {}
    for field in _CONNECTOR_FIELDS:
        incoming = body.get(field, "")
        if not incoming and stored:
            incoming = stored.get(field, "")
        connector[field] = incoming

    params = connector_to_params(hostname, connector)
    ok, message = test_connection(params)
    return jsonify({"ok": ok, "message": message})


# ---------------------------------------------------------------------------
# AI configuration
# ---------------------------------------------------------------------------
def ai_schema():
    """Return the provider schema so the settings form can be generated."""
    if not AI_AVAILABLE:
        return jsonify({"providers": {}})
    return jsonify({"providers": public_schema()})


def ai_settings():
    """Return current AI settings. Secrets are booleans, never values."""
    if not AI_AVAILABLE:
        return jsonify(
            {
                "enabled": False,
                "disclaimer_ok": False,
                "provider": "",
                "config": {},
                "active": False,
                "capabilities": {},
                "assistant_available": False,
                "available": False,
            }
        )
    store = get_ai_store()
    settings = store.get()
    capabilities = provider_capabilities(
        settings["provider"], settings["config"]
    )
    settings["available"] = True
    settings["active"] = store.is_active()
    settings["capabilities"] = capabilities.as_dict()
    settings["assistant_available"] = bool(
        settings["active"] and capabilities.supports_tools
    )
    return jsonify(settings)


def ai_save_settings():
    """Persist AI settings. Blank secret fields keep their stored value."""
    if not AI_AVAILABLE:
        abort(501, description="AI features are not installed in this build.")
    body = request.get_json(silent=True) or {}
    provider = (body.get("provider") or "").strip()
    if provider and provider not in PROVIDER_SCHEMA:
        abort(400, description="Unknown AI provider.")

    config = body.get("config")
    if config is not None and not isinstance(config, dict):
        abort(400, description="'config' must be an object.")

    store = get_ai_store()
    try:
        store.save(
            provider,
            config or {},
            enabled=body.get("enabled"),
            disclaimer_ok=body.get("disclaimer_ok"),
        )
    except ValueError as exc:
        abort(400, description=str(exc))

    capabilities = provider_capabilities(provider, config or {})
    return jsonify(
        {
            "ok": True,
            "active": store.is_active(),
            "capabilities": capabilities.as_dict(),
            "assistant_available": bool(
                store.is_active() and capabilities.supports_tools
            ),
        }
    )


def ai_test():
    """Test a provider configuration."""
    if not AI_AVAILABLE:
        abort(501, description="AI features are not installed in this build.")
    body = request.get_json(silent=True) or {}
    provider = (body.get("provider") or "").strip()
    if provider not in PROVIDER_SCHEMA:
        abort(400, description="Select a provider first.")

    submitted = body.get("config") or {}
    stored = get_ai_store().get(reveal=True)
    merged = dict(stored["config"]) if stored["provider"] == provider else {}
    for key, value in submitted.items():
        if str(value or "").strip():
            merged[key] = value

    ok, message = test_provider(provider, merged)
    return jsonify({"ok": ok, "message": message})


# ---------------------------------------------------------------------------
# Request guard
# ---------------------------------------------------------------------------
def _origin_guard():
    """Refuse requests that did not originate from our own page.

    Two separate checks:

    * **Host** — blocks DNS rebinding, where an attacker's domain resolves to
      127.0.0.1 so their page becomes same-origin. An origin allowlist alone
      does not catch that.
    * **Origin** — blocks cross-site requests. Note that a JSON POST would be
      stopped by CORS preflight anyway, but ``POST /api/open/log/<name>`` sends
      no body and no content type, making it a "simple request" that a hostile
      page could fire without preflight.
    """
    if request.path.startswith("/socket.io"):
        return  # handled by the engineio origin allowlist

    hosts = current_app.config.get("TERMINUS_HOSTS") or set()
    if (request.host or "").lower() not in hosts:
        logger.warning(
            "Refused request with Host %r for %s (expected one of %s)",
            request.host,
            request.path,
            sorted(hosts),
        )
        abort(403, description="Invalid Host header.")

    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc.lower() not in hosts:
        logger.warning(
            "Refused cross-origin request from %r for %s", origin, request.path
        )
        abort(403, description="Cross-origin request refused.")

    return


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------
def register_routes(app):
    """Bind every URL rule to the app (no blueprint, no auth)."""
    app.before_request(_origin_guard)
    rules = [
        ("/", "render", render, ["GET"]),
        ("/api/prefs", "get_prefs", get_prefs, ["GET"]),
        ("/api/prefs", "save_prefs", save_prefs, ["POST"]),
        ("/logs", "list_logs", list_logs, ["GET"]),
        ("/logs/view/<path:filename>", "view_log", view_log, ["GET"]),
        ("/logs", "delete_logs", delete_logs, ["DELETE"]),
        ("/api/connectors", "list_connectors", list_connectors, ["GET"]),
        ("/api/connectors/<name>", "get_connector", get_connector, ["GET"]),
        ("/api/connectors", "save_connector", save_connector, ["POST"]),
        (
            "/api/connectors/<name>",
            "delete_connector",
            delete_connector,
            ["DELETE"],
        ),
        ("/api/connectors/test", "test_connector", test_connector, ["POST"]),
        (
            "/api/open/session/<session_id>",
            "open_session_log",
            open_session_log,
            ["POST"],
        ),
        ("/api/open/log/<path:filename>", "open_log", open_log, ["POST"]),
        ("/api/open/folder", "open_log_folder", open_log_folder, ["POST"]),
        ("/api/shells", "list_shells", list_shells, ["GET"]),
        ("/api/ai/schema", "ai_schema", ai_schema, ["GET"]),
        ("/api/ai/settings", "ai_settings", ai_settings, ["GET"]),
        ("/api/ai/settings", "ai_save_settings", ai_save_settings, ["POST"]),
        ("/api/ai/test", "ai_test", ai_test, ["POST"]),
    ]
    for rule, endpoint, view_func, methods in rules:
        app.add_url_rule(rule, endpoint, view_func, methods=methods)
