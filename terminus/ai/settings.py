"""SQLite-backed store for AI provider configuration.

Provider-specific settings live in one JSON blob so adding a provider needs no
schema migration. Fields marked ``secret`` in
:data:`terminus.ai.providers.PROVIDER_SCHEMA` are encrypted individually with
the same Fernet key used for connector passwords.

Lives in the AI package so that ``terminus.credentials`` — which core code
depends on — has no dependency on the optional AI feature.

File path: terminus/ai/settings.py
"""

import json
import logging
import sqlite3

from ..app import DB_PATH, SECRET_KEY
from ..crypto import decrypt, encrypt
from .providers import PROVIDER_SCHEMA, secret_fields

logger = logging.getLogger(__name__)

_AI_SETTINGS_ROW = 1

# Bumped whenever the disclaimer's substance changes. Version 1 covered log
# analysis only; version 2 covers the Assistant proposing and running commands.
# A stored acceptance below this number is treated as not accepted.
DISCLAIMER_VERSION = 2

_store = None


def get_ai_store():
    """Return the process-wide :class:`AISettingsStore` singleton."""
    global _store
    if _store is None:
        _store = AISettingsStore(DB_PATH, SECRET_KEY)
    return _store


class AISettingsStore:
    """Single-row store for AI provider configuration.

    Provider-specific settings live in one JSON blob so adding a provider
    needs no schema migration. Fields marked ``secret`` in
    :data:`terminus.ai.PROVIDER_SCHEMA` are encrypted individually with the
    same Fernet key used for connector passwords.
    """

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
                    CREATE TABLE IF NOT EXISTS ai_settings (
                        id                 INTEGER PRIMARY KEY CHECK (id = 1),
                        enabled            INTEGER DEFAULT 0,
                        disclaimer_ok      INTEGER DEFAULT 0,
                        disclaimer_version INTEGER DEFAULT 0,
                        provider           TEXT DEFAULT '',
                        config             TEXT DEFAULT '{}'
                    );
                    """
                )
                cols = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(ai_settings)")
                }
                if "disclaimer_version" not in cols:
                    conn.execute(
                        "ALTER TABLE ai_settings "
                        "ADD COLUMN disclaimer_version INTEGER DEFAULT 0"
                    )
                    # An existing acceptance predates the Assistant. Clear it
                    # and disable the feature so nothing can run under consent
                    # the user never actually gave.
                    conn.execute(
                        "UPDATE ai_settings "
                        "   SET enabled = 0, disclaimer_ok = 0 "
                        " WHERE disclaimer_ok = 1"
                    )
                    logger.info(
                        "AI disclaimer superseded — re-consent required."
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO ai_settings (id) VALUES (?)",
                    (_AI_SETTINGS_ROW,),
                )
        except sqlite3.Error:
            logger.exception("Failed to initialise AI settings table.")
            raise

    def _row(self):
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM ai_settings WHERE id = ?", (_AI_SETTINGS_ROW,)
            ).fetchone()

    # -- public API ----------------------------------------------------------
    def get(self, reveal=False):
        """Return the stored settings.

        With ``reveal=False`` (the default) secret values are replaced by a
        boolean-ish marker so the UI can show "already set" without ever
        receiving the value. With ``reveal=True`` they are decrypted — used
        only when building a provider on the server side.

        ``disclaimer_ok`` is reported ``False`` when the stored acceptance
        predates :data:`DISCLAIMER_VERSION`, so a superseded consent cannot
        keep the feature alive.
        """
        row = self._row()
        if row is None:
            return {
                "enabled": False,
                "disclaimer_ok": False,
                "disclaimer_version": 0,
                "required_disclaimer_version": DISCLAIMER_VERSION,
                "provider": "",
                "config": {},
            }

        try:
            config = json.loads(row["config"] or "{}")
        except ValueError:
            logger.warning("Malformed AI config JSON — treating as empty.")
            config = {}

        provider = row["provider"] or ""
        secrets = set(secret_fields(provider))

        resolved = {}
        for key, value in config.items():
            if key in secrets:
                resolved[key] = self._dec(value) if reveal else bool(value)
            else:
                resolved[key] = value

        version = row["disclaimer_version"] or 0
        accepted = bool(row["disclaimer_ok"]) and version >= DISCLAIMER_VERSION

        return {
            "enabled": bool(row["enabled"]),
            "disclaimer_ok": accepted,
            "disclaimer_version": version,
            "required_disclaimer_version": DISCLAIMER_VERSION,
            "provider": provider,
            "config": resolved,
        }

    def save(self, provider, config, enabled=None, disclaimer_ok=None):
        """Persist settings, preserving blank secrets.

        A secret submitted empty keeps whatever is stored — same convention as
        connector passwords, since the browser never receives the real value.
        """
        if provider and provider not in PROVIDER_SCHEMA:
            raise ValueError(f"Unknown AI provider: {provider!r}")

        current = self._row()
        try:
            existing = json.loads(current["config"] or "{}") if current else {}
        except ValueError:
            existing = {}
        same_provider = (
            bool(current) and (current["provider"] or "") == provider
        )

        secrets = set(secret_fields(provider))
        stored = {}
        for key, value in (config or {}).items():
            text = "" if value is None else str(value)
            if key in secrets:
                if text:
                    stored[key] = self._enc(text)
                elif same_provider and existing.get(key):
                    stored[key] = existing[key]  # keep old ciphertext
                else:
                    stored[key] = ""
            else:
                stored[key] = text

        if disclaimer_ok is None:
            ok_flag = int(current["disclaimer_ok"]) if current else 0
            version = (current["disclaimer_version"] or 0) if current else 0
        elif disclaimer_ok:
            ok_flag, version = 1, DISCLAIMER_VERSION
        else:
            ok_flag, version = 0, 0

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE ai_settings
                   SET enabled = :enabled,
                       disclaimer_ok = :disclaimer_ok,
                       disclaimer_version = :disclaimer_version,
                       provider = :provider,
                       config = :config
                 WHERE id = :id
                """,
                {
                    "id": _AI_SETTINGS_ROW,
                    "provider": provider or "",
                    "config": json.dumps(stored),
                    "enabled": (
                        current["enabled"]
                        if enabled is None
                        else int(bool(enabled))
                    ),
                    "disclaimer_ok": ok_flag,
                    "disclaimer_version": version,
                },
            )

    def set_enabled(self, enabled):
        """Toggle the feature without touching provider settings."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE ai_settings SET enabled = ? WHERE id = ?",
                (int(bool(enabled)), _AI_SETTINGS_ROW),
            )

    def accept_disclaimer(self):
        """Record acceptance of the *current* disclaimer version."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE ai_settings "
                "   SET disclaimer_ok = 1, disclaimer_version = ? "
                " WHERE id = ?",
                (DISCLAIMER_VERSION, _AI_SETTINGS_ROW),
            )

    def is_active(self):
        """True only when enabled, disclaimed, and a provider is configured."""
        settings = self.get()
        return bool(
            settings["enabled"]
            and settings["disclaimer_ok"]
            and settings["provider"]
        )

    # -- encryption ----------------------------------------------------------
    def _enc(self, value):
        return encrypt(self.secret, value)

    def _dec(self, value):
        return decrypt(self.secret, value)
