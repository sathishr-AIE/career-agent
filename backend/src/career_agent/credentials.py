# backend/src/career_agent/credentials.py
"""Encrypted storage for company-site logins (site_credential). Passwords are
Fernet-encrypted at rest via security.py; list_() never returns a password or
ciphertext -- only put()/get() ever touch security.decrypt/encrypt."""
import secrets
import string
from urllib.parse import urlsplit

from career_agent.security import decrypt, encrypt

_UPPER, _LOWER, _DIGITS = string.ascii_uppercase, string.ascii_lowercase, string.digits
_SYMBOLS = "!@#$%^&*-_=+"  # site-friendly; no ambiguous look-alikes required


def normalize_domain(value: str) -> str:
    """A URL or bare host -> lowercase ASCII host, no scheme/userinfo/port/
    path/www/trailing dot. Raises ValueError when no host survives -- a
    caller must refuse and never store or look up a credential under a
    junk key.

    A raw netloc split (e.g. rsplit on "@") is not enough: a browser treats
    a backslash in a URL as a path separator, ending the host early, but
    Python's urlsplit does not -- "https://evil.com\\@careers.ses.com/"
    would parse as one netloc and rsplit("@") would hand back
    "careers.ses.com", the *attacker's* href-visible host being evil.com.
    Replacing "\\" with "/" first makes urlsplit end the netloc exactly
    where a browser would. hostname (not netloc) then drops userinfo and
    port and unwraps IPv6 brackets in one step, so "[::1]:8080" -> "::1"
    rather than the netloc's leading "[".
    """
    value = value.strip().lower().replace("\\", "/")
    if "//" not in value:
        value = "//" + value  # forces urlsplit to treat it as netloc, not path
    host = urlsplit(value).hostname
    if host:
        host = host.rstrip(".")
    if host and host.startswith("www."):
        host = host[4:]
    if not host:
        raise ValueError(f"no host found in domain/URL {value!r}")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"invalid host {host!r}") from exc


def generate_password(length: int = 20) -> str:
    """secrets-based password guaranteed to contain upper, lower, digit and a
    symbol -- reject-and-retry rather than forcing characters into fixed
    positions, which would leak position information."""
    if length < 4:
        raise ValueError("length must be at least 4 to fit all character classes")
    alphabet = _UPPER + _LOWER + _DIGITS + _SYMBOLS
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c in _UPPER for c in pw) and any(c in _LOWER for c in pw)
                and any(c in _DIGITS for c in pw) and any(c in _SYMBOLS for c in pw)):
            return pw


def put(conn, domain: str, login_url: str, email: str, password: str,
       created_by: str) -> int:
    """Upsert by normalized domain; encrypts before writing. Returns the row id."""
    d = normalize_domain(domain)
    enc = encrypt(password)
    row = conn.execute("SELECT id FROM site_credential WHERE domain = ?", (d,)).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE site_credential SET login_url = ?, email = ?, password_enc = ?, "
            "created_by = ? WHERE id = ?",
            (login_url, email, enc, created_by, row["id"]))
        conn.commit()
        return row["id"]
    cur = conn.execute(
        "INSERT INTO site_credential (domain, login_url, email, password_enc, created_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (d, login_url, email, enc, created_by))
    conn.commit()
    return cur.lastrowid


def get(conn, domain: str) -> dict | None:
    """Decrypted credential for a domain, or None. Bumps last_used_at --
    but only once decrypt() has actually succeeded, so a wrong
    CREDENTIAL_KEY (CredentialKeyError) never marks the row used."""
    d = normalize_domain(domain)
    row = conn.execute("SELECT * FROM site_credential WHERE domain = ?", (d,)).fetchone()
    if row is None:
        return None
    password = decrypt(row["password_enc"])
    conn.execute("UPDATE site_credential SET last_used_at = datetime('now') WHERE id = ?",
                (row["id"],))
    conn.commit()
    return {"id": row["id"], "domain": row["domain"], "login_url": row["login_url"],
            "email": row["email"], "password": password,
            "created_by": row["created_by"], "created_at": row["created_at"],
            "last_used_at": row["last_used_at"]}


def list_(conn) -> list[dict]:
    """Every stored login, metadata only -- never password or password_enc."""
    rows = conn.execute(
        "SELECT id, domain, login_url, email, created_by, created_at, last_used_at "
        "FROM site_credential ORDER BY domain").fetchall()
    return [dict(r) for r in rows]


def delete(conn, id: int) -> bool:
    cur = conn.execute("DELETE FROM site_credential WHERE id = ?", (id,))
    conn.commit()
    return cur.rowcount > 0
