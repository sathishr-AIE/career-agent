import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_agent import credentials, db
from career_agent.web import api_credentials
from career_agent.web import app as web


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_KEY", Fernet.generate_key().decode())


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    db.init_schema(db.connect(path))
    return path


@pytest.fixture
def conn(db_path):
    return db.connect(db_path)


@pytest.fixture
def client(db_path, monkeypatch):
    monkeypatch.setattr(web, "DB_PATH", db_path)
    app = FastAPI()
    app.include_router(api_credentials.router)
    return TestClient(app)


def test_get_logins_lists_metadata_never_passwords(client, conn):
    credentials.put(conn, "careers.ses.com", "https://careers.ses.com/login",
                    "asha@example.com", "s3cret-pw-XYZ", "agent")
    r = client.get("/api/logins")
    assert r.status_code == 200
    [item] = r.json()["items"]
    assert item["domain"] == "careers.ses.com"
    assert item["email"] == "asha@example.com"
    assert item["login_url"] == "https://careers.ses.com/login"
    assert item["created_by"] == "agent"
    assert item["created_at"] and "last_used_at" in item
    assert "s3cret-pw-XYZ" not in r.text and "password" not in r.text


def test_delete_login_and_unknown_id_is_404(client, conn):
    lid = credentials.put(conn, "a.com", "", "a@x.com", "pw", "user")
    assert client.delete(f"/api/logins/{lid}").json()["ok"]
    assert credentials.list_(conn) == []
    r = client.delete(f"/api/logins/{lid}")
    assert r.status_code == 404 and not r.json()["ok"]


# -- LG1: add a login you already have --------------------------------------

PW = "hunter2-Only-Mine"


def _post(client, **over):
    body = {"domain": "stripe.com", "login_url": "https://stripe.com/login",
            "email": "asha@example.com", "password": PW, **over}
    return client.post("/api/logins", json=body)


def test_add_login_saves_encrypted_and_never_echoes_the_password(client, conn):
    r = _post(client, domain="https://www.Stripe.com/jobs", email="  asha@example.com ")
    assert r.status_code == 200 and r.json()["ok"]
    assert r.json()["item"]["domain"] == "stripe.com"
    assert r.json()["item"]["created_by"] == "user"
    assert PW not in r.text and "password" not in r.text
    saved = credentials.get(conn, "stripe.com")
    assert (saved["email"], saved["password"]) == ("asha@example.com", PW)


def test_add_login_refuses_a_shared_host_and_writes_nothing(client, conn):
    r = _post(client, domain="boards.greenhouse.io", login_url="")
    assert r.status_code == 422
    message = r.json()["errors"]["domain"]
    assert "not a site" in message.lower() and "careers" in message, message
    assert PW not in r.text and credentials.list_(conn) == []


def test_add_login_requires_email_and_password(client, conn):
    r = _post(client, email=" ", password="")
    assert r.status_code == 422 and set(r.json()["errors"]) == {"email", "password"}
    assert credentials.list_(conn) == []


@pytest.mark.parametrize("url", ["http://stripe.com/login", "https://evil.com/login",
                                 "https://evil.com\@stripe.com/", "javascript:alert(1)"])
def test_add_login_sign_in_url_must_be_https_on_the_domain(client, conn, url):
    r = _post(client, login_url=url)
    assert r.status_code == 422 and "login_url" in r.json()["errors"]
    assert credentials.list_(conn) == []


def test_add_login_optional_sign_in_url_may_be_blank_or_a_subdomain(client, conn):
    assert _post(client, login_url="").status_code == 200
    assert _post(client, domain="ses.com", login_url="https://careers.ses.com/x").status_code == 200


def test_add_login_never_silently_replaces_an_existing_one(client, conn):
    credentials.put(conn, "stripe.com", "https://stripe.com/a", "old@example.com", "old-pw", "agent")
    r = _post(client)
    assert r.status_code == 409 and r.json()["exists"] and r.json()["created_by"] == "agent"
    assert credentials.get(conn, "stripe.com")["password"] == "old-pw"
    r = _post(client, replace=True)
    assert r.status_code == 200 and r.json()["item"]["created_by"] == "user"
    saved = credentials.get(conn, "stripe.com")
    assert (saved["email"], saved["password"]) == ("asha@example.com", PW)
    assert len(credentials.list_(conn)) == 1


def test_add_login_without_a_credential_key_saves_nothing(client, conn, monkeypatch):
    monkeypatch.delenv("CREDENTIAL_KEY")
    r = _post(client)
    assert r.status_code == 409 and "CREDENTIAL_KEY" in r.json()["message"]
    assert PW not in r.text and credentials.list_(conn) == []
