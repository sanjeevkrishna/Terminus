"""SSH connection service for Terminus.

Opens an interactive SSH channel via netcore's ``GenericHandler``, applies
``TCP_NODELAY`` for responsiveness, and optionally tees a clean log to disk.
This module is stateless — it holds no session state.

File path: terminus/services.py
"""
import logging
import socket
from datetime import datetime

from netcore import GenericHandler

logger = logging.getLogger(__name__)

_BANNER_RULE = "=" * 60


def connector_to_params(hostname, connector):
    """Map a stored connector dict + device hostname to connection params."""
    params = {
        "hostname": hostname,
        "username": connector.get("network_username", ""),
        "password": connector.get("network_password", ""),
    }
    if connector.get("jumphost_ip"):
        params["proxy"] = {
            "hostname": connector["jumphost_ip"],
            "username": connector.get("jumphost_username", ""),
            "password": connector.get("jumphost_password", ""),
        }
    return params


def _apply_tcp_nodelay(channel, tag=""):
    """Enable TCP_NODELAY on the channel's transport socket, if possible."""
    try:
        transport = channel.get_transport()
        sock = getattr(transport, "sock", None) if transport else None
        if sock:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return True
    except OSError as exc:
        logger.warning("[%s] TCP_NODELAY failed: %s", tag, exc)
    return False


def _open_logfile(logfile, hostname, base_prompt, tag=""):
    """Open a binary log file and write the session banner; return handle."""
    try:
        tee = open(logfile, "ab", buffering=0)
    except OSError as exc:
        logger.warning("[%s] logfile open failed: %s", tag, exc)
        return None

    banner = (
        f"{_BANNER_RULE}\n"
        f" Terminus Log\n"
        f" Host   : {hostname}\n"
        f" Prompt : {base_prompt}\n"
        f" Started: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"{_BANNER_RULE}\n\n"
    )
    tee.write(banner.encode())
    return tee


def open_terminus(params, logfile=None, tag=""):
    """Open an interactive SSH session.

    Returns ``(conn, channel, base_prompt, tee)`` where ``tee`` is an open
    binary file handle for logging, or ``None`` when no logfile is given.
    """
    kwargs = {
        "handler": "NETMIKO-TERMINAL",
        "hostname": params["hostname"],
        "username": params["username"],
        "password": params["password"],
        "device_type": params.get("device_type") or "autodetect",
        "read_timeout_override": 1000,
    }
    if params.get("proxy"):
        kwargs["proxy"] = params["proxy"]

    logger.debug("Terminus connect -> %s", kwargs["hostname"])
    conn = GenericHandler(**kwargs)

    channel = conn.channel.remote_conn
    channel.settimeout(0.0)
    _apply_tcp_nodelay(channel, tag)

    base_prompt = (getattr(conn, "base_prompt", None) or "session").strip()

    tee = _open_logfile(logfile, params["hostname"], base_prompt, tag) if logfile else None
    return conn, channel, base_prompt, tee


def test_connection(params, tag="test"):
    """Attempt a connection then immediately close it.

    Returns ``(ok: bool, message: str)`` for the Connectors panel's
    'Test connection' button.
    """
    conn = None
    try:
        conn, _channel, base_prompt, _tee = open_terminus(params, tag=tag)
        return True, f"Connected — prompt '{base_prompt}'."
    except Exception as exc:  # netcore raises a variety of exception types
        logger.info("[%s] test connection failed: %s", tag, exc)
        return False, str(exc)
    finally:
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:
                logger.debug("[%s] disconnect after test failed.", tag)