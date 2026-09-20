import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_agent import db, store
from career_agent.gate import MIN_FACTS_HARD
from career_agent.web import api_facts
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
    app.include_router(api_facts.router)
    return TestClient(app)


def _add(client, **over):
    body = {"claim": "Cut p95 latency 40%", "evidence": "Grafana dashboard", **over}
    return client.post("/api/facts", json=body)


def test_add_then_list_trims_and_defaults(client):
    r = _add(client, claim="  Cut p95 latency 40%  ", project="  ")
    assert r.status_code == 200 and r.json()["ok"]
    body = client.get("/api/facts").json()
    assert body["min_hard"] == MIN_FACTS_HARD
    [item] = body["items"]
    assert item["claim"] == "Cut p95 latency 40%"
    assert item["project"] is None and item["confidence"] == "high"


def test_add_blank_claim_or_bad_confidence_is_422_and_stores_nothing(client, conn):
    r = _add(client, claim="  ", confidence="certain")
    assert r.status_code == 422
    assert set(r.json()["errors"]) == {"claim", "confidence"}
    assert store.fact_list(conn) == []


def test_facts_feed_the_gate_text(client, conn):
    _add(client)
    assert store.facts(conn) == ["Cut p95 latency 40% (evidence: Grafana dashboard)"]


def test_edit_saves_and_unknown_id_is_404(client, conn):
    fid = _add(client).json()["id"]
    r = client.put(f"/api/facts/{fid}", json={"claim": "New", "evidence": "PR #12",
                                               "confidence": "medium"})
    assert r.status_code == 200
    assert store.fact_list(conn)[0]["claim"] == "New"
    assert client.put("/api/facts/999", json={"claim": "x", "evidence": "y"}).status_code == 404


def test_delete_removes_and_unknown_id_is_404(client, conn):
    fid = _add(client).json()["id"]
    assert client.delete(f"/api/facts/{fid}").status_code == 200
    assert store.fact_list(conn) == []
    assert client.delete("/api/facts/999").status_code == 404


def test_delete_refuses_a_fact_a_tailored_resume_cites(client, conn):
    fid = _add(client).json()["id"]
    conn.execute("INSERT INTO resume (version, path, content) VALUES (?, ?, ?)",
                 ("tailored-1", "x.docx",
                  json.dumps({"bullets": [{"text": "b", "fact_ids": [fid]}]})))
    conn.commit()
    r = client.delete(f"/api/facts/{fid}")
    assert r.status_code == 409 and "tailored-1" in r.json()["message"]
    assert len(store.fact_list(conn)) == 1
    # The list names the same versions the refusal does (FC1).
    assert client.get("/api/facts").json()["items"][0]["cited_by"] == ["tailored-1"]


def test_list_says_which_resumes_cite_each_fact(client, conn):
    """FC1: the page shows the citation before a delete is refused for it."""
    cited = _add(client).json()["id"]
    _add(client, claim="Second claim")
    conn.execute("INSERT INTO resume (version, path, content) VALUES (?, ?, ?)",
                 ("tailored-1", "x.docx",
                  json.dumps({"bullets": [{"text": "b", "fact_ids": [cited]}]})))
    conn.commit()

    body = client.get("/api/facts").json()

    assert [i["cited_by"] for i in body["items"]] == [["tailored-1"], []]
    assert body["min_hard"] == MIN_FACTS_HARD and body["min_warn"]
