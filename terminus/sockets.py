"""Socket.IO handlers, SSH session lifecycle, and shared session state.

All handlers live under the ``/terminus`` namespace. Background tasks
capture the socketio instance passed to :func:`register_socket_handlers`.
No Flask app context or auth is required (local desktop app).

The process-wide session/log store also lives here — sockets own the
session lifecycle, and :mod:`terminus.routes` reads it via :func:`get_state`.

File path: terminus/sockets.py
"""
import logging
import os
import re
from datetime import datetime

from flask import request
from flask_socketio import join_room

from .app import LOG_DIR, LOCAL_SHELL, LOCAL_SHELL_LABEL
from .shell import open_local_shell
from .credentials import get_store
from .services import connector_to_params, open_terminus

logger = logging.getLogger(__name__)

NS = "/terminus"
_RECV_BYTES = 65536
_POLL_INTERVAL = 0.002
_BANNER_RULE = "=" * 60

# ---------------------------------------------------------------------------
# Shared state (single-process, single-user → a module-level dict is enough)
# ---------------------------------------------------------------------------
_state = {"sessions": {}, "logs": {}}


def get_state():
    """Return the shared ``{'sessions': {...}, 'logs': {...}}`` store."""
    return _state


# ---------------------------------------------------------------------------
# Text / log helpers
# ---------------------------------------------------------------------------
_ANSI_RE = re.compile(
    rb"\x1b\[[0-9;?]*[ -/]*[@-~]"
    rb"|\x1b\][^\x07]*\x07"
    rb"|\x1b[@-Z]"
    rb"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]",
    re.VERBOSE,
)


def _strip_ansi_bytes(data: bytes) -> bytes:
    data = _ANSI_RE.sub(b"", data)
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _term_text(msg: str) -> str:
    return msg.replace("\r\n", "\n").replace("\n", "\r\n")


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("_") or "session"


def _write_footer_and_close(tee):
    if not tee:
        return
    try:
        tee.write(
            f"\n\n{_BANNER_RULE}\n"
            f" Session Ended: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"{_BANNER_RULE}\n".encode()
        )
        tee.flush()
    except OSError:
        pass
    finally:
        try:
            tee.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------
def register_socket_handlers(socketio):
    """Attach all Terminus namespace handlers to the given SocketIO."""

    def emit_output(session_id, data, normalize=False):
        if normalize:
            data = _term_text(data)
        socketio.emit(
            "output", {"session_id": session_id, "data": data},
            namespace=NS, to=session_id,
        )

    def emit_status(session_id, msg):
        emit_output(session_id, msg, normalize=True)

    def emit_ended(session_id):
        socketio.emit(
            "session_ended", {"session_id": session_id},
            namespace=NS, to=session_id,
        )

    def close_session(sess):
        if not sess:
            return
        _write_footer_and_close(sess.get("tee"))
        try:
            sess["conn"].disconnect()
        except Exception:
            logger.debug("Disconnect failed for a closing session.")

    # -- background tasks ----------------------------------------------------
    def do_connect(session_id, sid, params, log_dir):
        ts = datetime.now().strftime("%Y-%m-%d_%H.%M.%S")
        logpath = os.path.join(log_dir, f"{_safe_name(params['hostname'])}_{ts}.log")
        try:
            conn, channel, base_prompt, tee = open_terminus(
                params, logfile=logpath, tag=session_id
            )
            download_name = (
                f"{_safe_name(base_prompt)}_{datetime.now():%Y-%m-%d_%H.%M}.log"
            )
            _state["sessions"][session_id] = {
                "conn": conn, "channel": channel, "sid": sid,
                "logpath": logpath, "log_dir": log_dir,
                "tee": tee, "log_started": False,
            }
            _state["logs"][session_id] = {
                "path": logpath, "download_name": download_name,
            }
            socketio.emit("session_ready", {
                "session_id": session_id,
                "base_prompt": base_prompt,
                "device_type": getattr(conn, "device_type", ""),
                "logname": download_name,
            }, namespace=NS, to=session_id)
            emit_status(session_id, f"*** Connected ({base_prompt}) ***\n")
            socketio.start_background_task(read_output, session_id, channel)
        except Exception as exc:
            logger.info("Connection failed for %s: %s", session_id, exc)
            emit_status(session_id, f"\n*** Connection failed: {exc} ***\n")
            emit_ended(session_id)

    def do_open_shell(session_id, sid):
        os.makedirs(LOG_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H.%M.%S")
        logpath = os.path.join(LOG_DIR, f"local-shell_{ts}.log")
        try:
            conn, channel, base_prompt, tee = open_local_shell(
                LOCAL_SHELL, LOCAL_SHELL_LABEL, logfile=logpath, tag=session_id
            )
            download_name = f"local-shell_{datetime.now():%Y-%m-%d_%H.%M}.log"
            _state["sessions"][session_id] = {
                "conn": conn, "channel": channel, "sid": sid,
                "logpath": logpath, "log_dir": LOG_DIR,
                "tee": tee, "log_started": True,  # local shells: log from the start
            }
            _state["logs"][session_id] = {
                "path": logpath, "download_name": download_name,
            }
            socketio.emit("session_ready", {
                "session_id": session_id,
                "base_prompt": base_prompt,
                "device_type": "local-shell",
                "logname": download_name,
            }, namespace=NS, to=session_id)
            emit_status(session_id, f"*** {base_prompt} ***\n")
            socketio.start_background_task(read_output, session_id, channel)
        except Exception as exc:
            logger.info("Local shell failed for %s: %s", session_id, exc)
            emit_status(session_id, f"\n*** Local shell failed: {exc} ***\n")
            emit_ended(session_id)

    def _tee_write(sess, tee, data):
        """Strip ANSI, drop leading blank output on first write, tee to disk."""
        clean = _strip_ansi_bytes(data)
        if sess and not sess.get("log_started"):
            stripped = clean.lstrip(b"\r\n \t")
            if not stripped:
                return
            clean = stripped
            sess["log_started"] = True
        if clean:
            try:
                tee.write(clean)
            except OSError:
                pass

    def read_output(session_id, channel):
        while True:
            try:
                if channel.recv_ready():
                    data = channel.recv(_RECV_BYTES)
                    if not data:
                        break
                    sess = _state["sessions"].get(session_id)
                    tee = (sess or {}).get("tee")
                    if tee:
                        _tee_write(sess, tee, data)
                    socketio.emit(
                        "output",
                        {"session_id": session_id, "data": data.decode(errors="ignore")},
                        namespace=NS, to=session_id,
                    )
                    continue
                if channel.closed:
                    break
                socketio.sleep(_POLL_INTERVAL)
            except Exception:
                break

        sess = _state["sessions"].pop(session_id, None)
        if sess:
            _write_footer_and_close(sess.get("tee"))
        emit_status(session_id, "\n*** Connection closed ***\n")
        emit_ended(session_id)

    # -- socket handlers -----------------------------------------------------
    @socketio.on("connect", namespace=NS)
    def on_connect():
        # Local desktop app — no auth. Accept all connections.
        return None

    @socketio.on("join", namespace=NS)
    def on_join(data):
        join_room(data["session_id"])

    @socketio.on("ssh_connect", namespace=NS)
    def on_ssh_connect(data):
        session_id = data["session_id"]
        if session_id in _state["sessions"]:
            emit_status(session_id, "\n*** Session already active ***\n")
            return

        hostname = (data.get("hostname") or "").strip()
        connector_name = data.get("connector")
        if not hostname or not connector_name:
            emit_status(session_id, "\n*** Missing hostname or connector ***\n")
            emit_ended(session_id)
            return

        connector = get_store().get(connector_name)
        if not connector:
            emit_status(session_id, "\n*** Unknown connector ***\n")
            emit_ended(session_id)
            return

        params = connector_to_params(hostname, connector)
        os.makedirs(LOG_DIR, exist_ok=True)
        socketio.start_background_task(
            do_connect, session_id, request.sid, params, LOG_DIR
        )

    @socketio.on("open_shell", namespace=NS)
    def on_open_shell(data):
        session_id = data["session_id"]
        if session_id in _state["sessions"]:
            emit_status(session_id, "\n*** Session already active ***\n")
            return
        socketio.start_background_task(do_open_shell, session_id, request.sid)

    @socketio.on("input", namespace=NS)
    def on_input(data):
        sess = _state["sessions"].get(data["session_id"])
        if sess and sess["channel"].send_ready():
            try:
                sess["channel"].send(data["data"])
            except Exception:
                pass

    @socketio.on("resize", namespace=NS)
    def on_resize(data):
        sess = _state["sessions"].get(data["session_id"])
        if not sess:
            return
        try:
            sess["channel"].resize_pty(
                width=int(data["cols"]), height=int(data["rows"])
            )
        except Exception:
            pass

    @socketio.on("close_session", namespace=NS)
    def on_close(data):
        close_session(_state["sessions"].pop(data["session_id"], None))

    @socketio.on("disconnect", namespace=NS)
    def on_disconnect():
        sid = request.sid
        stale = [k for k, v in _state["sessions"].items() if v["sid"] == sid]
        for key in stale:
            close_session(_state["sessions"].pop(key, None))