"""Fernet encryption helpers for values stored at rest.

Shared by the connector store and the AI settings store. Takes the secret as a
parameter rather than importing it, so this module depends on nothing inside
the package.

File path: terminus/crypto.py
"""

import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


@lru_cache(maxsize=4)
def fernet(secret: str) -> Fernet:
    """Return a cached Fernet for *secret* (key derivation is not free)."""
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt(secret: str, plaintext: str) -> str:
    """Return Fernet ciphertext (empty in -> empty out)."""
    if not plaintext:
        return ""
    return fernet(secret).encrypt(plaintext.encode()).decode()


def decrypt(secret: str, token: str) -> str:
    """Return plaintext for a Fernet token (empty in -> empty out).

    Returns an empty string if the token cannot be decrypted (e.g. the key
    changed), rather than raising — a bad stored secret shouldn't break the
    whole lookup.
    """
    if not token:
        return ""
    try:
        return fernet(secret).decrypt(token.encode()).decode()
    except InvalidToken:
        logger.warning("Failed to decrypt a stored secret (key mismatch?).")
        return ""
