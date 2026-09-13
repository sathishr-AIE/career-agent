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
    """A URL or bare host -> lowercase host, no scheme/userinfo/port/path/www."""
    value = value.strip().lower()
    if "//" not in value:
        value = "//" + value  # forces urlsplit to treat it as netloc, not path
    netloc = urlsplit(value).netloc or value
    host = netloc.rsplit("@", 1)[-1].split(":", 1)[0]  # strip userinfo, port
    return host[4:] if host.startswith("www.") else host


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
    """Decrypted credential for a domain, or None. Bumps last_used_at."""
    d = normalize_domain(domain)
    row = conn.execute("SELECT * FROM site_credential WHERE domain = ?", (d,)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE site_credential SET last_used_at = datetime('now') WHERE id = ?",
                (row["id"],))
    conn.commit()
    return {"id": row["id"], "domain": row["domain"], "login_url": row["login_url"],
            "email": row["email"], "password": decrypt(row["password_enc"]),
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
