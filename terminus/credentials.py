"""SQLite-backed connector (credential) store.

Passwords are encrypted at rest with Fernet, keyed off the app secret.
Connections are short-lived (open / commit / close per operation) so the
store is safe to use from both the HTTP and Socket.IO worker threads.
Blank password fields on upsert preserve the existing stored value.

File path: terminus/credentials.py
"""
import base64
import hashlib
import logging
import sqlite3
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from .app import DB_PATH, SECRET_KEY

logger = logging.getLogger(__name__)

_PW_FIELDS = ("network_password", "jumphost_password")
_ALL_FIELDS = ("network_username", "network_password", "jumphost_ip", "jumphost_username", "jumphost_password",)

_store = None

# ---------------------------------------------------------------------------
# Encryption helpers (cached Fernet — key derivation is not free)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=4)
def _fernet(secret: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def _encrypt(secret: str, plaintext: str) -> str:
    """Return Fernet ciphertext (empty in -> empty out)."""
    if not plaintext:
        return ""
    return _fernet(secret).encrypt(plaintext.encode()).decode()


def _decrypt(secret: str, token: str) -> str:
    """Return plaintext for a Fernet token (empty in -> empty out).

    Returns an empty string if the token cannot be decrypted (e.g. the key
    changed), rather than raising — a bad stored secret shouldn't break the
    whole connector lookup.
    """
    if not token:
        return ""
    try:
        return _fernet(secret).decrypt(token.encode()).decode()
    except InvalidToken:
        logger.warning("Failed to decrypt a stored secret (key mismatch?).")
        return ""


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
                        jumphost_password TEXT DEFAULT ''
                    );
                    """
                )
        except sqlite3.Error:
            logger.exception("Failed to initialise credential database.")
            raise

    def _enc(self, value):
        return _encrypt(self.secret, value)

    def _dec(self, value):
        return _decrypt(self.secret, value)

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
                        values[field] = self._enc(incoming)
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
                     jumphost_ip, jumphost_username, jumphost_password)
                VALUES
                    (:name, :network_username, :network_password,
                     :jumphost_ip, :jumphost_username, :jumphost_password)
                ON CONFLICT(name) DO UPDATE SET
                    network_username  = excluded.network_username,
                    network_password  = excluded.network_password,
                    jumphost_ip       = excluded.jumphost_ip,
                    jumphost_username = excluded.jumphost_username,
                    jumphost_password = excluded.jumphost_password;
                """,
                {"name": name, **values},
            )

    def delete(self, name):
        """Delete a connector; return ``True`` if a row was removed."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM connectors WHERE name = ?", (name,))
            return cur.rowcount > 0

    def has_jumphost(self, name):
        """UI hint: whether a jump host is configured (no secret exposure)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT jumphost_ip FROM connectors WHERE name = ?", (name,)
            ).fetchone()
        return bool(row and row["jumphost_ip"])