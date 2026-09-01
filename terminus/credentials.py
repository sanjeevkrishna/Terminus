"""SQLite-backed connector (credential) store.

Passwords are encrypted at rest with Fernet, keyed off the app secret.
Connections are short-lived (open / commit / close per operation) so the store
is safe to use from both the HTTP and Socket.IO worker threads. Blank password
fields on upsert preserve the existing stored value.

File path: terminus/credentials.py
"""

import logging
import sqlite3

from .app import DB_PATH, SECRET_KEY
from .crypto import decrypt, encrypt

logger = logging.getLogger(__name__)

_PW_FIELDS = (
    "network_password",
    "jumphost_password",
)
_ALL_FIELDS = (
    "network_username",
    "network_password",
    "jumphost_ip",
    "jumphost_username",
    "jumphost_password",
    "device_type",
    "ssh_options",
)

_store = None


# ---------------------------------------------------------------------------
# Store singleton
# ---------------------------------------------------------------------------
def get_store():
    """Return the process-wide :class:`CredentialStore` singleton."""
    global _store
    if _store is None:
        _store = CredentialStore(DB_PATH, SECRET_KEY)
    return _store


class CredentialStore:
    """CRUD over the ``connectors`` table with encrypted password columns."""

    def __init__(self, db_path, secret):
        self.db_path = db_path
        self.secret = secret
        self._init_db()

    # -- infra ---------------------------------------------------------------
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self):
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS connectors (
                        name              TEXT PRIMARY KEY,
                        network_username  TEXT DEFAULT '',
                        network_password  TEXT DEFAULT '',
                        jumphost_ip       TEXT DEFAULT '',
                        jumphost_username TEXT DEFAULT '',
                        jumphost_password TEXT DEFAULT '',
                        device_type       TEXT DEFAULT '',
                        ssh_options       TEXT DEFAULT ''
                    );
                    """
                )
                cols = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(connectors)")
                }
                for col in ("device_type", "ssh_options"):
                    if col not in cols:
                        conn.execute(
                            f"ALTER TABLE connectors ADD COLUMN {col} TEXT DEFAULT ''"
                        )
        except sqlite3.Error:
            logger.exception("Failed to initialise credential database.")
            raise

    def _enc(self, value):
        return encrypt(self.secret, value)

    def _dec(self, value):
        return decrypt(self.secret, value)

    # -- public API ----------------------------------------------------------
    def list_names(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name FROM connectors ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [r["name"] for r in rows]

    def get(self, name):
        """Return a decrypted connector dict, or ``None`` if absent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM connectors WHERE name = ?", (name,)
            ).fetchone()
        if not row:
            return None
        connector = {field: row[field] for field in _ALL_FIELDS}
        for pw in _PW_FIELDS:
            connector[pw] = self._dec(connector[pw])
        return connector

    def upsert(self, name, connector):
        """Insert or update. Blank password fields keep the existing value."""
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM connectors WHERE name = ?", (name,)
            ).fetchone()

            values = {}
            for field in _ALL_FIELDS:
                incoming = connector.get(field, "")
                if field in _PW_FIELDS:
                    if incoming:
                        values[field] = encrypt(self.secret, incoming)
                    elif existing is not None:
                        values[field] = existing[field]  # keep old ciphertext
                    else:
                        values[field] = ""
                else:
                    values[field] = incoming

            conn.execute(
                """
                INSERT INTO connectors
                    (name, network_username, network_password,
                     jumphost_ip, jumphost_username, jumphost_password,
                     device_type, ssh_options)
                VALUES
                    (:name, :network_username, :network_password,
                     :jumphost_ip, :jumphost_username, :jumphost_password,
                     :device_type, :ssh_options)
                ON CONFLICT(name) DO UPDATE SET
                    network_username  = excluded.network_username,
                    network_password  = excluded.network_password,
                    jumphost_ip       = excluded.jumphost_ip,
                    jumphost_username = excluded.jumphost_username,
                    jumphost_password = excluded.jumphost_password,
                    device_type       = excluded.device_type,
                    ssh_options       = excluded.ssh_options;
                """,
                {"name": name, **values},
            )

    def delete(self, name):
        """Delete a connector; return ``True`` if a row was removed."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM connectors WHERE name = ?", (name,)
            )
            return cur.rowcount > 0

    def has_jumphost(self, name):
        """UI hint: whether a jump host is configured (no secret exposure)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT jumphost_ip FROM connectors WHERE name = ?", (name,)
            ).fetchone()
        return bool(row and row["jumphost_ip"])
