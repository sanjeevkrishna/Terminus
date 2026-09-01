"""Shared session-log file opener.

Both the SSH layer and the local-shell layer open a buffered log and write a
header banner; only the header fields differ.

File path: terminus/logbanner.py
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

BANNER_RULE = "=" * 60

# Buffered: SessionLog flushes explicitly on snapshot/close, and unbuffered
# writes cost a syscall per line on large outputs.
LOG_BUFFER = 256 * 1024


def open_session_logfile(path, title, fields, tag=""):
    """Open *path* for append and write a banner; return the handle or None.

    *fields* is an ordered mapping of label -> value, rendered aligned beneath
    the title. ``Started`` is appended automatically.
    """
    try:
        tee = open(path, "ab", buffering=LOG_BUFFER)
    except OSError as exc:
        logger.warning("[%s] logfile open failed: %s", tag, exc)
        return None

    rows = dict(fields or {})
    rows["Started"] = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    width = max((len(label) for label in rows), default=0)

    lines = [BANNER_RULE, f" {title}"]
    lines += [
        f" {label.ljust(width)} : {value}" for label, value in rows.items()
    ]
    lines += [BANNER_RULE, "", ""]

    tee.write("\n".join(lines).encode())
    tee.flush()  # land the banner immediately
    return tee
