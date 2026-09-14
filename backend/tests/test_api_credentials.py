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
