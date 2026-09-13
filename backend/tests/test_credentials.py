# backend/tests/test_credentials.py
import re

import pytest
from cryptography.fernet import Fernet

from career_agent import credentials, db
from career_agent.security import CredentialKeyError, decrypt, encrypt, load_key


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    return c


@pytest.fixture(autouse=True)
def key(monkeypatch):
    """Every test gets its own generated key -- never the real .env."""
    k = Fernet.generate_key().decode()
    monkeypatch.setenv("CREDENTIAL_KEY", k)
    return k


# -- security.py --------------------------------------------------------

def test_encrypt_decrypt_round_trip():
    token = encrypt("hunter2")
    assert token != "hunter2"
    assert decrypt(token) == "hunter2"


def test_encrypt_is_randomised():
    assert encrypt("hunter2") != encrypt("hunter2")


def test_load_key_missing_raises_with_generation_command(monkeypatch):
    monkeypatch.delenv("CREDENTIAL_KEY", raising=False)
    with pytest.raises(CredentialKeyError) as exc:
        load_key()
    msg = str(exc.value)
    assert "Fernet.generate_key" in msg
    assert ".env" in msg


def test_load_key_malformed_raises(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_KEY", "not-a-fernet-key")
    with pytest.raises(CredentialKeyError):
        load_key()


def test_decrypt_with_wrong_key_raises_credential_key_error(monkeypatch):
    token = encrypt("hunter2")
    monkeypatch.setenv("CREDENTIAL_KEY", Fernet.generate_key().decode())
    with pytest.raises(CredentialKeyError):
        decrypt(token)


def test_generate_password_length_and_character_classes():
    for _ in range(200):
        pw = credentials.generate_password()
        assert len(pw) == 20
        assert any(c.isupper() for c in pw)
        assert any(c.islower() for c in pw)
        assert any(c.isdigit() for c in pw)
        assert any(c in "!@#$%^&*-_=+" for c in pw)


def test_generate_password_custom_length():
    assert len(credentials.generate_password(length=32)) == 32


# -- normalize_domain -----------------------------------------------------

@pytest.mark.parametrize("value", [
    "https://www.Careers.SES.com/login",
    "careers.ses.com",
    "http://careers.ses.com/apply?x=1",
    "CAREERS.SES.COM",
    "careers.ses.com:443",
])
def test_normalize_domain_collapses_to_same_host(value):
    assert credentials.normalize_domain(value) == "careers.ses.com"


def test_normalize_domain_backslash_spoof_resolves_to_the_real_pre_backslash_host():
    """A browser treats "\\" as a path separator, ending the netloc there, so
    "https://evil.com\\@careers.ses.com/" is really evil.com -- not
    careers.ses.com, even though a naive "@"-rsplit on the raw netloc would
    say so."""
    assert (credentials.normalize_domain("https://evil.com\\@careers.ses.com/")
           == "evil.com")


def test_normalize_domain_userinfo_at_sign_strips_to_host():
    assert credentials.normalize_domain("https://user:pw@careers.ses.com/") == "careers.ses.com"


def test_normalize_domain_ipv6_literal():
    assert credentials.normalize_domain("https://[::1]:8080/") == "::1"


def test_normalize_domain_strips_trailing_dot():
    assert credentials.normalize_domain("careers.ses.com.") == "careers.ses.com"
    assert credentials.normalize_domain("https://www.careers.ses.com./x") == "careers.ses.com"


def test_normalize_domain_unicode_host_becomes_punycode():
    assert credentials.normalize_domain("https://café.com/") == "xn--caf-dma.com"


@pytest.mark.parametrize("value", ["", "https:///x", "   ", "https://\\@/x"])
def test_normalize_domain_with_no_host_raises_value_error(value):
    with pytest.raises(ValueError):
        credentials.normalize_domain(value)


def test_put_and_get_refuse_a_domain_with_no_host_rather_than_store_it(conn):
    with pytest.raises(ValueError):
        credentials.put(conn, "https:///x", "https://x/login", "u@x.com", "pw",
                        created_by="agent")
    assert conn.execute("SELECT COUNT(*) n FROM site_credential").fetchone()["n"] == 0
    with pytest.raises(ValueError):
        credentials.get(conn, "https:///x")


# -- put / get / list_ / delete -------------------------------------------

def test_put_then_get_round_trips_password(conn):
    credentials.put(conn, "careers.ses.com", "https://careers.ses.com/login",
                    "asha@example.com", "s3cret!Pw", created_by="agent")
    row = credentials.get(conn, "careers.ses.com")
    assert row["password"] == "s3cret!Pw"
    assert row["email"] == "asha@example.com"
    assert row["domain"] == "careers.ses.com"


def test_stored_password_is_encrypted_and_varies_across_puts(conn):
    credentials.put(conn, "a.com", "https://a.com", "u@a.com", "s3cret!Pw",
                    created_by="agent")
    enc1 = conn.execute("SELECT password_enc FROM site_credential WHERE domain='a.com'"
                        ).fetchone()["password_enc"]
    assert enc1 != "s3cret!Pw"

    credentials.put(conn, "b.com", "https://b.com", "u@b.com", "s3cret!Pw",
                    created_by="agent")
    enc2 = conn.execute("SELECT password_enc FROM site_credential WHERE domain='b.com'"
                        ).fetchone()["password_enc"]
    assert enc1 != enc2  # random Fernet IV -- same plaintext, different ciphertext


def test_get_bumps_last_used_at(conn):
    credentials.put(conn, "a.com", "https://a.com", "u@a.com", "pw", created_by="agent")
    before = credentials.list_(conn)[0]["last_used_at"]
    assert before is None
    credentials.get(conn, "a.com")
    after = credentials.list_(conn)[0]["last_used_at"]
    assert after is not None


def test_get_missing_domain_returns_none(conn):
    assert credentials.get(conn, "nowhere.com") is None


def test_get_with_wrong_key_raises_and_does_not_bump_last_used_at(conn, monkeypatch):
    credentials.put(conn, "a.com", "https://a.com", "u@a.com", "pw", created_by="agent")
    monkeypatch.setenv("CREDENTIAL_KEY", Fernet.generate_key().decode())
    with pytest.raises(CredentialKeyError):
        credentials.get(conn, "a.com")
    row = conn.execute("SELECT last_used_at FROM site_credential WHERE domain='a.com'"
                       ).fetchone()
    assert row["last_used_at"] is None


def test_put_upserts_by_normalized_domain(conn):
    id1 = credentials.put(conn, "https://www.Careers.SES.com/login",
                          "https://www.careers.ses.com/login",
                          "old@example.com", "oldpw", created_by="agent")
    id2 = credentials.put(conn, "careers.ses.com", "https://careers.ses.com/login",
                          "new@example.com", "newpw", created_by="user")
    assert id1 == id2
    rows = credentials.list_(conn)
    assert len(rows) == 1
    row = credentials.get(conn, "careers.ses.com")
    assert row["email"] == "new@example.com"
    assert row["password"] == "newpw"
    assert row["created_by"] == "user"


def test_list_never_contains_password_fields(conn):
    credentials.put(conn, "a.com", "https://a.com", "u@a.com", "s3cret!Pw",
                    created_by="agent")
    rows = credentials.list_(conn)
    assert len(rows) == 1
    assert "password" not in rows[0]
    assert "password_enc" not in rows[0]
    assert set(rows[0]) == {"id", "domain", "login_url", "email", "created_by",
                            "created_at", "last_used_at"}


def test_created_by_check_rejects_other_values(conn):
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO site_credential (domain, email, password_enc, created_by) "
            "VALUES ('x.com', 'a@x.com', 'enc', 'robot')")
        conn.commit()


def test_delete_returns_true_then_false(conn):
    cid = credentials.put(conn, "a.com", "https://a.com", "u@a.com", "pw",
                          created_by="agent")
    assert credentials.delete(conn, cid) is True
    assert credentials.delete(conn, cid) is False
    assert credentials.list_(conn) == []


# -- Task 15: host match and the user-row guard ------------------------------

@pytest.mark.parametrize("url,domain,ok", [
    ("https://careers.ses.com/login", "careers.ses.com", True),
    ("https://www.careers.ses.com/x", "careers.ses.com", True),
    ("https://jobs.careers.ses.com/x", "careers.ses.com", True),
    ("https://evil.com/login", "linkedin.com", False),
    ("https://notlinkedin.com/", "linkedin.com", False),       # no dot boundary
    ("https://linkedin.com.evil.com/", "linkedin.com", False),
    ("https://evil.com\\@linkedin.com/", "linkedin.com", False),
    ("", "linkedin.com", False),
    ("https://linkedin.com:8443/login", "linkedin.com", True),     # port
    ("https://linkedin.com./login", "linkedin.com", True),         # trailing dot
    ("https://bücher.de/login", "xn--bcher-kva.de", True),         # IDNA
    ("https://linkedin%2ecom/login", "linkedin.com", False),       # percent in host
    ("https://link\tedin.com/login", "linkedin.com", False),       # tab
    ("https://linked\x01in.com/", "linkedin.com", False),          # control char
])
def test_host_matches_only_on_the_domain_or_a_dot_boundary_subdomain(url, domain, ok):
    assert credentials.host_matches(url, domain) is ok


def test_an_agent_put_never_overwrites_any_existing_login(conn):
    credentials.put(conn, "ses.com", "", "me@x.com", "users-own-pw", "user")
    credentials.put(conn, "b.com", "", "a@x.com", "agent-pw", "agent")
    for domain in ("ses.com", "b.com"):
        with pytest.raises(credentials.LoginExists):
            credentials.put(conn, domain, "", "agent@x.com", "generated", "agent")
    assert credentials.get(conn, "ses.com")["password"] == "users-own-pw"
    assert credentials.get(conn, "b.com")["password"] == "agent-pw"


@pytest.mark.parametrize("domain", ["careers.ses.com", "ses.wd3.myworkdayjobs.com",
                                    "acme.wd103.myworkdayjobs.com", "bbc.co.uk",
                                    "acme.vercel.app", "boards.greenhouse.io",
                                    "https://www.careers.ses.com/join"])
def test_account_domain_accepts_a_real_site(domain):
    assert credentials.account_domain(domain) == credentials.normalize_domain(domain)


@pytest.mark.parametrize("domain", [
    "com", "localhost", "127.0.0.1", "[::1]", "127.1", "0x7f.1", "0x7f000001",
    "co.in", "www.co.in", "co.uk", "ac.uk", "gov.in", "org.in", "gov.uk", "com.sg", "com.br",
    "myworkdayjobs.com", "wd3.myworkdayjobs.com", "acme.jobs.myworkdayjobs.com",
    "github.io", "greenhouse.io", "lever.co", "successfactors.com", "vercel.app",
    "netlify.app", "pages.dev", "web.app", "firebaseapp.com", "herokuapp.com",
    "azurewebsites.net", "blogspot.com"])
def test_account_domain_refuses_bare_ip_and_shared_suffixes(domain):
    with pytest.raises(ValueError):
        credentials.account_domain(domain)
