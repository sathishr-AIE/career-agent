# backend/src/career_agent/security.py
"""Fernet encryption for site_credential passwords. The key is read from the
environment at call time (never cached at import time) so a test can set/unset
CREDENTIAL_KEY per case via monkeypatch without process restarts, and so a
missing key fails loudly on first use rather than silently at import."""
import os

from cryptography.fernet import Fernet, InvalidToken

_GEN_CMD = ('python -c "from cryptography.fernet import Fernet; '
           'print(Fernet.generate_key().decode())"')
_HELP = (f"Generate one with:\n  {_GEN_CMD}\n"
        "and put it in backend/.env as CREDENTIAL_KEY=<value>.")


class CredentialKeyError(Exception):
    """CREDENTIAL_KEY is missing, malformed, or doesn't match the key a
    stored credential was encrypted with. Never wraps the raw ciphertext or
    plaintext -- only says what's wrong with the key."""


def load_key() -> bytes:
    raw = os.environ.get("CREDENTIAL_KEY")
    if not raw:
        raise CredentialKeyError(f"CREDENTIAL_KEY is not set. {_HELP}")
    try:
        Fernet(raw.encode())
    except Exception as exc:
        raise CredentialKeyError(
            f"CREDENTIAL_KEY is not a valid Fernet key. {_HELP}") from exc
    return raw.encode()


def encrypt(plaintext: str) -> str:
    return Fernet(load_key()).encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return Fernet(load_key()).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise CredentialKeyError(
            "Could not decrypt this credential -- CREDENTIAL_KEY does not "
            "match the key it was encrypted with.") from exc
