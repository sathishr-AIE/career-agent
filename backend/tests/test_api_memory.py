import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_agent import db, store
from career_agent.web import api_memory
from career_agent.web import app as web


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    db.init_schema(conn)
    return path


@pytest.fixture
def conn(db_path):
    return db.connect(db_path)


@pytest.fixture
def client(db_path, monkeypatch):
    monkeypatch.setattr(web, "DB_PATH", db_path)
    app = FastAPI()
    app.include_router(api_memory.router)
    return TestClient(app)


def test_get_memory_lists_keyed_and_untwinned_literal_rows(client, conn):
    store.qa_remember(conn, "Notice period?", "30 days", memory_key="notice_period",
                      source_job_id=1)
    store.qa_upsert(conn, "Years of experience?", "6", is_volatile=False)

    r = client.get("/api/memory")
    assert r.status_code == 200
    items = r.json()["items"]
    labels = {i["label"] for i in items}
    assert "notice_period" in labels
    assert store.qa_normalize("Years of experience?") in labels
    assert store.qa_normalize("Notice period?") not in labels, "literal twin hidden"


def test_put_memory_trims_and_saves(client, conn):
    store.qa_upsert(conn, "Years of experience?", "6", is_volatile=False)
    row_id = store.qa_lookup(conn, "Years of experience?")["id"]

    r = client.put(f"/api/memory/{row_id}", json={"answer": "  7  ", "is_volatile": True})
    assert r.status_code == 200 and r.json()["ok"]
    row = store.qa_lookup(conn, "Years of experience?")
    assert row["answer"] == "7"
    assert row["is_volatile"] == 1


def test_put_memory_blank_answer_is_422(client, conn):
    store.qa_upsert(conn, "Years of experience?", "6", is_volatile=False)
    row_id = store.qa_lookup(conn, "Years of experience?")["id"]
    r = client.put(f"/api/memory/{row_id}", json={"answer": "   ", "is_volatile": False})
    assert r.status_code == 422
    assert store.qa_lookup(conn, "Years of experience?")["answer"] == "6"


def test_put_memory_unknown_id_is_404(client, conn):
    r = client.put("/api/memory/999", json={"answer": "x", "is_volatile": False})
    assert r.status_code == 404


def test_delete_memory_removes_the_row(client, conn):
    store.qa_upsert(conn, "Years of experience?", "6", is_volatile=False)
    row_id = store.qa_lookup(conn, "Years of experience?")["id"]
    r = client.delete(f"/api/memory/{row_id}")
    assert r.status_code == 200 and r.json()["ok"]
    assert store.qa_lookup(conn, "Years of experience?") is None


def test_delete_memory_unknown_id_is_404(client, conn):
    r = client.delete("/api/memory/999")
    assert r.status_code == 404
