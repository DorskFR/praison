"""Symmetric encryption for the Praise bearer token at rest.

The token must survive a round-trip (encrypt at rest, decrypt to replay to the
Praise server on each fetch), so this is reversible symmetric encryption, not
hashing. The key comes from ``PRAISON_SECRET_KEY`` (a urlsafe-base64 32-byte
Fernet key); for local/standalone use a key is generated once and persisted
0600 under the config dir.
"""

import base64
import hashlib
import hmac
import os
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet

from praison.config import DEFAULT_CONFIG_DIR

_SECRET_ENV = "PRAISON_SECRET_KEY"  # noqa: S105 - env var name, not a secret
_SESSION_SECRET_ENV = "PRAISON_SESSION_SECRET"  # noqa: S105 - env var name, not a secret
DEFAULT_KEY_PATH = DEFAULT_CONFIG_DIR / "secret.key"


def _load_or_create_key(key_path: Path = DEFAULT_KEY_PATH) -> bytes:
    """Return the Fernet key: from env if set, else a persisted local key."""
    env_key = os.environ.get(_SECRET_ENV)
    if env_key:
        return env_key.encode()
    if key_path.is_file():
        return key_path.read_bytes().strip()
    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)
    key_path.chmod(0o600)
    return key


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(_load_or_create_key())


def encrypt(plaintext: str) -> str:
    """Encrypt a secret for storage at rest."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a secret previously produced by :func:`encrypt`."""
    return _fernet().decrypt(token.encode()).decode()


def session_secret() -> str:
    """Signing secret for the praison web session cookie.

    Kept cryptographically separate from the Fernet encryption key: an explicit
    ``PRAISON_SESSION_SECRET`` wins, otherwise it is derived from the master key
    via a keyed hash with a domain-separation label. A leaked cookie-signing
    secret therefore does not reveal the key used to encrypt credentials at rest.
    """
    override = os.environ.get(_SESSION_SECRET_ENV)
    if override:
        return override
    derived = hmac.new(_load_or_create_key(), b"praison-session-cookie-v1", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(derived).decode()
