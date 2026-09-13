# backend/src/career_agent/credentials.py
"""Encrypted storage for company-site logins (site_credential). Passwords are
Fernet-encrypted at rest via security.py; list_() never returns a password or
ciphertext -- only put()/get() ever touch security.decrypt/encrypt."""
import ipaddress
import re
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
    value = value.strip()
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("whitespace or a control character in a domain/URL")
    value = value.lower().replace("\\", "/")
    if "//" not in value:
        value = "//" + value  # forces urlsplit to treat it as netloc, not path
    host = urlsplit(value).hostname
    if host:
        host = host.rstrip(".")
    if host and host.startswith("www."):
        host = host[4:]
    if not host:
        raise ValueError(f"no host found in domain/URL {value!r}")
    if "%" in host:
        raise ValueError(f"percent-encoding in host {host!r}")
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


class LoginExists(Exception):
    """An agent write would replace an existing login (user- or agent-made):
    an approved account is only ever created, never re-keyed."""


# Public suffixes (a registrable name sits directly left of one) and shared
# platforms (every tenant gets a subdomain): a login saved under either would
# be offered to every site on it. A tenant host under a platform
# (acme.vercel.app) is fine. ponytail: a hand-kept list; the upgrade path is
# the Public Suffix List (publicsuffix.org) once a miss shows up.
_SHARED_SUFFIXES = {
    "co.in", "co.uk", "com.au", "ac.uk", "gov.in", "org.in", "gov.uk", "com.sg", "com.br",
    "github.io", "myworkdayjobs.com", "greenhouse.io", "lever.co", "icims.com",
    "smartrecruiters.com", "taleo.net", "successfactors.com", "ashbyhq.com",
    "vercel.app", "netlify.app", "pages.dev", "web.app", "firebaseapp.com",
    "herokuapp.com", "azurewebsites.net", "blogspot.com"}
# Workday tenants live at <tenant>.wd<N>.myworkdayjobs.com; wd<N> alone is shared.
_WORKDAY_TENANT = re.compile(r"[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com")
# A browser reads an all-numeric/hex host as an IPv4 address (127.1, 0x7f.1).
_NUMERIC_LABEL = re.compile(r"\d+|0x[0-9a-f]*")


def account_domain(value: str) -> str:
    """normalize_domain, refusing a key no login may be saved or used under: a
    bare label, localhost, an IP literal or any all-numeric/hex host, a shared
    suffix itself (checked after the www. strip, so www.co.in is co.in), or a
    Workday host that is not a tenant's. Raises ValueError."""
    d = normalize_domain(value)
    labels = d.split(".")
    try:
        ipaddress.ip_address(d)
        numeric = True
    except ValueError:
        numeric = all(_NUMERIC_LABEL.fullmatch(label) for label in labels)
    workday = d == "myworkdayjobs.com" or d.endswith(".myworkdayjobs.com")
    if (numeric or len(labels) < 2 or d == "localhost" or d.endswith(".localhost")
            or d in _SHARED_SUFFIXES or (workday and not _WORKDAY_TENANT.fullmatch(d))):
        raise ValueError(f"not a site an account can be saved for: {d!r}")
    return d


def host_matches(page_url: str, domain: str) -> bool:
    """True when page_url's host is `domain` or a subdomain of it on a dot
    boundary. Both sides go through normalize_domain, so case, www, port and
    backslash tricks can't make another host read as the stored domain."""
    try:
        host, d = normalize_domain(page_url), normalize_domain(domain)
    except ValueError:
        return False
    return host == d or host.endswith("." + d)


def secure_url(url) -> bool:
    """https, or http only for a local server: never javascript:, file:, or a
    bare host. A backslash ends the host, as in a browser (normalize_domain)."""
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url.strip().replace("\\", "/"))
        host = parts.hostname
    except ValueError:
        return False
    return bool(host) and (parts.scheme == "https" or (
        parts.scheme == "http" and host in ("localhost", "127.0.0.1", "::1")))


def put(conn, domain: str, login_url: str, email: str, password: str,
       created_by: str) -> int:
    """Upsert by normalized domain; encrypts before writing. Returns the row id.
    An "agent" write needs an account_domain and never replaces an existing
    row (LoginExists)."""
    d = account_domain(domain) if created_by == "agent" else normalize_domain(domain)
    row = conn.execute("SELECT id, created_by FROM site_credential WHERE domain = ?",
                       (d,)).fetchone()
    if row is not None and created_by == "agent":
        raise LoginExists(d)
    enc = encrypt(password)
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
