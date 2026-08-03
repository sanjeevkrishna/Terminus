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
import time

from flask import abort, jsonify, render_template, request, send_file

from .app import LOG_DIR, PREFS_PATH
from .credentials import get_store
from .services import connector_to_params, test_connection
from .sockets import get_state

logger = logging.getLogger(__name__)

_CONNECTOR_FIELDS = (
    "network_username", "network_password",
    "jumphost_ip", "jumphost_username", "jumphost_password",
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
    """Best-effort flush + fsync of a live session's tee before download."""
    tee = sess.get("tee")
    if not tee or not hasattr(tee, "flush"):
        return
    try:
        tee.flush()
        if callable(getattr(tee, "fileno", None)):
            os.fsync(tee.fileno())
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
def render():
    prefs = _read_prefs()
    return render_template("terminus.html", theme=prefs.get("theme", "dark"))


# ---------------------------------------------------------------------------
# Session log download
# ---------------------------------------------------------------------------
def download_log(session_id):
    state = get_state()
    entry = state["logs"].get(session_id)
    sessions = state["sessions"]

    if not entry:
        if session_id in sessions:
            abort(409, description="Log not ready yet — session still initializing.")
        abort(404, description="No log for this session.")

    real_log = os.path.realpath(entry["path"])
    download_name = entry.get("download_name") or os.path.basename(real_log)

    sess = sessions.get(session_id)
    log_dir = (sess or {}).get("log_dir") or _log_dir()
    if not real_log.startswith(os.path.realpath(log_dir) + os.sep):
        abort(403, description="Invalid path.")
    if not os.path.isfile(real_log):
        abort(404, description="Log file not found on disk.")

    if sess:
        _flush_session_log(sess)

    return send_file(
        real_log, as_attachment=True,
        download_name=download_name, mimetype="text/plain",
    )


# ---------------------------------------------------------------------------
# Preferences (theme / font) — persisted server-side, launcher-independent
# ---------------------------------------------------------------------------
def _read_prefs():
    try:
        with open(PREFS_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def get_prefs():
    return jsonify(_read_prefs())


def save_prefs():
    body = request.get_json(silent=True) or {}
    prefs = _read_prefs()
    for key in ("theme", "font"):
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
    files = []
    for path in glob.glob(os.path.join(log_dir, "*.log")):
        try:
            st = os.stat(path)
        except OSError:
            continue
        files.append({
            "filename": os.path.basename(path),
            "created": int(st.st_ctime),
            "created_str": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(st.st_ctime)
            ),
            "size": st.st_size,
        })
    files.sort(key=lambda f: f["created"], reverse=True)
    return jsonify({"logs": files})


def view_log(filename):
    full = _safe_resolve(_log_dir(), filename)
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

    return jsonify({
        "ok": True, "deleted": deleted, "skipped": skipped, "errors": errors,
    })


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
# Route registration
# ---------------------------------------------------------------------------
def register_routes(app):
    """Bind every URL rule to the app (no blueprint, no auth)."""
    rules = [
        ("/",                          "render",           render,           ["GET"]),
        ("/download/<session_id>",     "download_log",     download_log,     ["GET"]),
        ("/api/prefs",                 "get_prefs",        get_prefs,        ["GET"]),
        ("/api/prefs",                 "save_prefs",       save_prefs,       ["POST"]),
        ("/logs",                      "list_logs",        list_logs,        ["GET"]),
        ("/logs/view/<path:filename>", "view_log",         view_log,         ["GET"]),
        ("/logs",                      "delete_logs",      delete_logs,      ["DELETE"]),
        ("/api/connectors",            "list_connectors",  list_connectors,  ["GET"]),
        ("/api/connectors/<name>",     "get_connector",    get_connector,    ["GET"]),
        ("/api/connectors",            "save_connector",   save_connector,   ["POST"]),
        ("/api/connectors/<name>",     "delete_connector", delete_connector, ["DELETE"]),
        ("/api/connectors/test",       "test_connector",   test_connector,   ["POST"]),
    ]
    for rule, endpoint, view_func, methods in rules:
        app.add_url_rule(rule, endpoint, view_func, methods=methods)