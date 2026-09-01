"""Socket.IO handlers, session lifecycle, and shared session state.

All handlers live under the ``/terminus`` namespace. Background tasks capture
the socketio instance passed to :func:`register_socket_handlers`. No Flask app
context or auth is required (local desktop app).

The read loop is the only reader of any channel; it fans incoming bytes out to
the client, the session log/transcript (:mod:`terminus.transcript`) and, when
the Assistant is running a command, an executor capture
(:mod:`terminus.executor`).

File path: terminus/sockets.py
"""

import logging
import os
import re
import select
import threading
import time
from datetime import datetime

from flask import request
from flask_socketio import join_room

from .app import LAUNCH_TOKEN, LOG_DIR, shell_by_id
from .credentials import get_store
from .services import connector_to_params, open_terminus
from .shell import open_local_shell
from .transcript import SessionLog, SessionMonitor, SessionTranscript

try:
    from .ai import agent, get_ai_store

    AI_AVAILABLE = True
except ImportError:
    agent = None
    get_ai_store = None
    AI_AVAILABLE = False
    logging.getLogger(__name__).info(
        "AI package unavailable — the Assistant is disabled."
    )

logger = logging.getLogger(__name__)

NS = "/terminus"
_BANNER_RULE = "=" * 60

# -- reader loop ------------------------------------------------------------
_RECV_BYTES = 65536
_SELECT_TIMEOUT = 0.05  # socket wait; PTY channels block internally
# Output is coalesced before emitting: bulk transfers get batched, while the
# time cap keeps interactive echo feeling instant.
_EMIT_FLUSH_BYTES = 16384
_EMIT_FLUSH_SECS = 0.03


# -- escape handling --------------------------------------------------------
# Terminal report replies (DA1/DA2, cursor position, device status). xterm
# sends these answering a shell's query; a PTY-backed shell has no use for
# them and they surface as junk on the next command line.
_TERM_REPORT_RE = re.compile(r"\x1b\[(?:\?[\d;]*c|[\d;]*[cnR])")


# ---------------------------------------------------------------------------
# Shared state (single-process, single-user → a module-level dict is enough)
# ---------------------------------------------------------------------------
_state = {"sessions": {}, "logs": {}}


def get_state():
    """Return the shared ``{'sessions': {...}, 'logs': {...}}`` store."""
    return _state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _term_text(msg: str) -> str:
    """Convert bare newlines to CRLF for terminal display."""
    return msg.replace("\r\n", "\n").replace("\n", "\r\n")


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("_") or "session"


def _selectable(channel):
    """Return *channel* if ``select()`` can wait on it, else ``None``.

    Paramiko channels expose ``fileno()``; the PTY wrapper does not and blocks
    inside ``recv_ready()`` instead.
    """
    try:
        fd = channel.fileno()
    except Exception:
        return None
    return channel if isinstance(fd, int) and fd >= 0 else None


def _snapshot_session_log(sess):
    """Push a session's buffered output to disk."""
    session_log = (sess or {}).get("session_log")
    if session_log:
        session_log.snapshot()


def _write_footer_and_close(tee):
    if not tee:
        return
    try:
        tee.write(
            (
                f"\n{_BANNER_RULE}\n"
                f" Session Ended: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                f"{_BANNER_RULE}\n"
            ).encode()
        )
        tee.flush()
    except (OSError, ValueError):
        pass
    finally:
        try:
            tee.close()
        except (OSError, ValueError):
            pass


def _finalize_log(sess):
    """Flush the session log, write the footer, close the file."""
    session_log = sess.get("session_log")
    if session_log:
        session_log.close()
    _write_footer_and_close(sess.get("tee"))


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------
def register_socket_handlers(socketio):
    """Attach all Terminus namespace handlers to the given SocketIO."""

    # -- emit helpers --------------------------------------------------------
    def emit_output(session_id, data, normalize=False):
        if normalize:
            data = _term_text(data)
        socketio.emit(
            "output",
            {"session_id": session_id, "data": data},
            namespace=NS,
            to=session_id,
        )

    def emit_status(session_id, msg):
        emit_output(session_id, msg, normalize=True)

    def emit_ready(session_id, base_prompt, device_type, logname):
        socketio.emit(
            "session_ready",
            {
                "session_id": session_id,
                "base_prompt": base_prompt,
                "device_type": device_type,
                "logname": logname,
            },
            namespace=NS,
            to=session_id,
        )

    def emit_ended(session_id):
        socketio.emit(
            "session_ended",
            {"session_id": session_id},
            namespace=NS,
            to=session_id,
        )

    def close_session(sess):
        if not sess:
            return
        # An in-flight capture will otherwise wait out its full timeout on a
        # channel that no longer exists.
        sess["capture"] = None
        _finalize_log(sess)
        try:
            sess["conn"].disconnect()
        except Exception:
            logger.debug("Disconnect failed for a closing session.")

    def register_session(
        session_id,
        sid,
        conn,
        channel,
        tee,
        logpath,
        hostname,
        device_type,
        download_name,
        base_prompt="",
    ):
        """Record a live session, its log, transcript and executor slots."""
        transcript = SessionTranscript()
        _state["sessions"][session_id] = {
            "conn": conn,
            "channel": channel,
            "sid": sid,
            "logpath": logpath,
            "log_dir": LOG_DIR,
            "hostname": hostname,
            "device_type": device_type,
            "base_prompt": base_prompt,
            "tee": tee,
            "transcript": transcript,
            "session_log": SessionLog(tee, transcript),
            # -- executor contract (see terminus/executor.py) --
            "monitor": SessionMonitor(),
            "exec_lock": threading.Lock(),
            "capture": None,
            "ai_width_set": False,
        }
        _state["logs"][session_id] = {
            "path": logpath,
            "download_name": download_name,
        }

    # -- background tasks ----------------------------------------------------
    def do_connect(session_id, sid, params):
        ts = datetime.now().strftime("%Y-%m-%d_%H.%M.%S")
        logpath = os.path.join(
            LOG_DIR, f"{_safe_name(params['hostname'])}_{ts}.log"
        )
        try:
            conn, channel, base_prompt, tee = open_terminus(
                params, logfile=logpath, tag=session_id
            )
        except Exception as exc:
            logger.info("Connection failed for %s: %s", session_id, exc)
            emit_status(session_id, f"\n*** Connection failed: {exc} ***\n")
            emit_ended(session_id)
            return

        device_type = getattr(conn, "device_type", "")
        download_name = (
            f"{_safe_name(base_prompt)}_{datetime.now():%Y-%m-%d_%H.%M}.log"
        )
        register_session(
            session_id,
            sid,
            conn,
            channel,
            tee,
            logpath,
            params["hostname"],
            device_type,
            download_name,
            base_prompt=base_prompt,
        )

        emit_ready(session_id, base_prompt, device_type, download_name)
        emit_status(
            session_id,
            (
                f"{_BANNER_RULE}\n"
                f" Terminus\n"
                f" Host   : {params['hostname']}\n"
                f" Prompt : {base_prompt}\n"
                f" Started: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                f"{_BANNER_RULE}\n"
            ),
        )
        socketio.start_background_task(read_output, session_id, channel)

    def do_open_shell(session_id, sid, shell_id):
        shell = shell_by_id(shell_id)
        ts = datetime.now().strftime("%Y-%m-%d_%H.%M.%S")
        logpath = os.path.join(LOG_DIR, f"{_safe_name(shell['id'])}_{ts}.log")
        try:
            conn, channel, base_prompt, tee = open_local_shell(
                shell["command"],
                shell["label"],
                logfile=logpath,
                tag=session_id,
            )
        except Exception as exc:
            logger.info("Local shell failed for %s: %s", session_id, exc)
            emit_status(session_id, f"\n*** Local shell failed: {exc} ***\n")
            emit_ended(session_id)
            return

        download_name = (
            f"{_safe_name(shell['id'])}_{datetime.now():%Y-%m-%d_%H.%M}.log"
        )
        register_session(
            session_id,
            sid,
            conn,
            channel,
            tee,
            logpath,
            shell["label"],
            "local-shell",
            download_name,
            base_prompt=base_prompt,
        )

        emit_ready(session_id, base_prompt, "local-shell", download_name)
        socketio.start_background_task(read_output, session_id, channel)

    def read_output(session_id, channel):
        """Stream channel output to the client until the session ends."""
        selectable = _selectable(channel)
        pending = []
        pending_len = 0
        last_flush = time.monotonic()

        def flush():
            nonlocal pending_len, last_flush
            if pending:
                socketio.emit(
                    "output",
                    {"session_id": session_id, "data": "".join(pending)},
                    namespace=NS,
                    to=session_id,
                )
                pending.clear()
                pending_len = 0
            last_flush = time.monotonic()

        while True:
            try:
                if selectable is not None:
                    # Socket: wait on the fd rather than spinning.
                    try:
                        ready, _, _ = select.select(
                            [selectable], [], [], _SELECT_TIMEOUT
                        )
                    except (OSError, ValueError):
                        break
                    available = bool(ready) or channel.recv_ready()
                else:
                    # PTY: recv_ready() blocks internally for up to ~50 ms.
                    available = channel.recv_ready()

                if available:
                    data = channel.recv(_RECV_BYTES)
                    if data:
                        sess = _state["sessions"].get(session_id)
                        if sess:
                            # Order matters: the monitor drives preflight and
                            # must be exact, so update it first and
                            # synchronously.
                            sess["monitor"].note(data)
                            session_log = sess.get("session_log")
                            if session_log:
                                session_log.feed(data)
                            capture = sess.get("capture")
                            if capture is not None:
                                capture.feed(data)
                        pending.append(data.decode(errors="ignore"))
                        pending_len += len(data)
                        if (
                            pending_len >= _EMIT_FLUSH_BYTES
                            or time.monotonic() - last_flush
                            >= _EMIT_FLUSH_SECS
                        ):
                            flush()
                        continue

                if pending:
                    flush()
                if channel.closed:
                    break
            except Exception:
                logger.debug(
                    "Read loop ended for %s.", session_id, exc_info=True
                )
                break

        flush()
        sess = _state["sessions"].pop(session_id, None)
        if sess:
            _finalize_log(sess)
        emit_status(
            session_id,
            (
                f"\n{_BANNER_RULE}\n"
                f" Session Ended: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                f"{_BANNER_RULE}\n"
            ),
        )
        emit_ended(session_id)

    def _ai_ready(chat_id=None):
        """False (and tell the client) when the AI package is unavailable."""
        if AI_AVAILABLE:
            return True
        if chat_id:
            socketio.emit(
                "chat_error",
                {
                    "chat_id": chat_id,
                    "message": "AI features are not installed in this build.",
                },
                namespace=NS,
                to=chat_id,
            )
        return False

    # -- Assistant chat ------------------------------------------------------
    def _chat_emit(event, chat_id=None, **payload):
        socketio.emit(
            event, {"chat_id": chat_id, **payload}, namespace=NS, to=chat_id
        )
        socketio.sleep(0)  # let streamed deltas flush promptly

    def _get_session(session_id):
        return _state["sessions"].get(session_id)

    def _notify_terminal(session_id, text):
        """Echo an AI command marker into the user's own terminal."""
        emit_output(session_id, text)

    def _conversation(chat_id, sid):
        return agent.get_or_create(
            chat_id, sid, _chat_emit, _get_session, _notify_terminal
        )

    @socketio.on("chat_start", namespace=NS)
    def on_chat_start(data):
        chat_id = data.get("chat_id")
        if not chat_id:
            return
        join_room(chat_id)
        if not _ai_ready(chat_id):
            return
        conv = _conversation(chat_id, request.sid)
        conv.auto_approve_read_only = bool(data.get("auto_approve"))
        conv.set_state(agent.STATE_IDLE)

    @socketio.on("chat_send", namespace=NS)
    def on_chat_send(data):
        chat_id = data.get("chat_id")
        text = (data.get("text") or "").strip()
        if not chat_id or not text:
            logger.warning(
                "chat_send ignored — chat_id=%r len=%d", chat_id, len(text)
            )
            return
        join_room(chat_id)
        if not _ai_ready(chat_id):
            return

        logger.info(
            "chat_send: chat=%s sessions=%s len=%d",
            chat_id,
            data.get("session_ids"),
            len(text),
        )
        _chat_emit("chat_ack", chat_id=chat_id)

        if not get_ai_store().is_active():
            _chat_emit(
                "chat_error",
                chat_id=chat_id,
                message="AI is not enabled. Configure a provider "
                "under Settings → AI.",
            )
            return

        conv = _conversation(chat_id, request.sid)
        if conv.busy:
            _chat_emit(
                "chat_error",
                chat_id=chat_id,
                message="A question is already in progress.",
            )
            return

        # Only sessions that still exist server-side; the agent aliases these
        # in order, so a stale id would shift every alias.
        session_ids = [
            sid
            for sid in (data.get("session_ids") or [])
            if sid in _state["sessions"]
        ]
        socketio.start_background_task(agent.run_turn, conv, text, session_ids)

    @socketio.on("chat_approve", namespace=NS)
    def on_chat_approve(data):
        if not AI_AVAILABLE:
            return
        conv = agent.get(data.get("chat_id"))
        if not conv:
            return
        if not conv.decide(data.get("plan_id"), True, items=data.get("items")):
            _chat_emit(
                "chat_error",
                chat_id=conv.chat_id,
                message="That approval is no longer pending.",
            )

    @socketio.on("chat_deny", namespace=NS)
    def on_chat_deny(data):
        if not AI_AVAILABLE:
            return
        conv = agent.get(data.get("chat_id"))
        if conv:
            conv.decide(
                data.get("plan_id"), False, reason=data.get("reason") or ""
            )

    @socketio.on("chat_cancel", namespace=NS)
    def on_chat_cancel(data):
        if not AI_AVAILABLE:
            return
        conv = agent.get(data.get("chat_id"))
        if conv:
            conv.cancel()

    @socketio.on("chat_reset", namespace=NS)
    def on_chat_reset(data):
        if not AI_AVAILABLE:
            return
        conv = agent.get(data.get("chat_id"))
        if conv:
            conv.reset()

    @socketio.on("chat_auto_approve", namespace=NS)
    def on_chat_auto_approve(data):
        if not AI_AVAILABLE:
            return
        conv = agent.get(data.get("chat_id"))
        if conv:
            conv.auto_approve_read_only = bool(data.get("enabled"))

    # -- session handlers ----------------------------------------------------
    @socketio.on("connect", namespace=NS)
    def on_connect(auth):
        """Reject any handshake without this launch's token.

        The origin allowlist is the primary control; this also stops a second
        Terminus instance, or a page left open across a restart, from driving
        this one.
        """
        token = auth.get("token") if isinstance(auth, dict) else None
        if not token or token != LAUNCH_TOKEN:
            logger.warning(
                "Refused socket %s: %s. Usually a browser tab left open "
                "across a restart — reload it.",
                request.sid,
                "no token" if not token else "stale token",
            )
            return False
        return None

    @socketio.on("join", namespace=NS)
    def on_join(data):
        session_id = data.get("session_id")
        if session_id:
            join_room(session_id)

    @socketio.on("ssh_connect", namespace=NS)
    def on_ssh_connect(data):
        session_id = data.get("session_id")
        if not session_id:
            return
        if session_id in _state["sessions"]:
            emit_status(session_id, "\n*** Session already active ***\n")
            return

        hostname = (data.get("hostname") or "").strip()
        connector_name = data.get("connector")
        # Join here rather than trusting a separate client 'join' — output can
        # be emitted before that event is processed, and room emits to an
        # unjoined room are silently dropped.
        join_room(session_id)

        if not hostname or not connector_name:
            emit_status(
                session_id, "\n*** Missing hostname or connector ***\n"
            )
            emit_ended(session_id)
            return

        connector = get_store().get(connector_name)
        if not connector:
            emit_status(session_id, "\n*** Unknown connector ***\n")
            emit_ended(session_id)
            return

        os.makedirs(LOG_DIR, exist_ok=True)
        socketio.start_background_task(
            do_connect,
            session_id,
            request.sid,
            connector_to_params(hostname, connector),
        )

    @socketio.on("open_shell", namespace=NS)
    def on_open_shell(data):
        session_id = data.get("session_id")
        if not session_id:
            return
        if session_id in _state["sessions"]:
            emit_status(session_id, "\n*** Session already active ***\n")
            return
        # A local shell emits within milliseconds, so join before starting.
        join_room(session_id)
        os.makedirs(LOG_DIR, exist_ok=True)
        socketio.start_background_task(
            do_open_shell, session_id, request.sid, data.get("shell")
        )

    @socketio.on("input", namespace=NS)
    def on_input(data):
        session_id = data.get("session_id")
        sess = _state["sessions"].get(session_id)
        if not sess or not sess["channel"].send_ready():
            return

        # A capture owns the channel: interleaved keystrokes would corrupt the
        # output the Assistant is waiting on.
        if sess.get("capture") is not None:
            socketio.emit(
                "input_blocked",
                {
                    "session_id": session_id,
                    "message": "The Assistant is running a command on this "
                    "session.",
                },
                namespace=NS,
                to=session_id,
            )
            return

        payload = data.get("data") or ""
        if "\x1b" in payload:
            payload = _TERM_REPORT_RE.sub("", payload)
            if not payload:
                return

        session_log = sess.get("session_log")
        if session_log:
            session_log.note_input()

        if "\n" in payload or "\r" in payload:
            payload = payload.replace("\r\n", "\r").replace("\n", "\r")
        try:
            sess["channel"].send(payload)
        except Exception:
            logger.debug("Input send failed.", exc_info=True)

    @socketio.on("resize", namespace=NS)
    def on_resize(data):
        sess = _state["sessions"].get(data.get("session_id"))
        if not sess:
            return
        try:
            cols, rows = int(data["cols"]), int(data["rows"])
        except (KeyError, TypeError, ValueError):
            return
        try:
            sess["channel"].resize_pty(width=cols, height=rows)
        except Exception:
            logger.debug("Channel resize failed.", exc_info=True)
        session_log = sess.get("session_log")
        if session_log:
            session_log.resize(cols, rows)

    @socketio.on("close_session", namespace=NS)
    def on_close(data):
        close_session(_state["sessions"].pop(data.get("session_id"), None))

    @socketio.on("disconnect", namespace=NS)
    def on_disconnect():
        sid = request.sid
        # Drop conversations *before* sessions: a turn blocked in
        # _await_decision must be woken and abandoned, not left to execute
        # against channels that are being torn down underneath it.
        if AI_AVAILABLE:
            agent.drop_for_sid(sid)
        agent.drop_for_sid(sid)
        stale = [k for k, v in _state["sessions"].items() if v["sid"] == sid]
        for key in stale:
            close_session(_state["sessions"].pop(key, None))
