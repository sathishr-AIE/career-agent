import pytest
from fastapi.testclient import TestClient

from career_agent import chat, db
from career_agent.web import app as web


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    db.init_schema(conn)
    conn.execute("INSERT INTO job (fingerprint, source, external_id, company,"
                 " company_normalized, title, title_normalized, url)"
                 " VALUES ('fp1','ats','1','Acme','acme','AI Engineer',"
                 " 'aiengineer','https://x/1')")
    conn.execute("INSERT INTO assessment (job_id, stage, role_fit, credibility,"
                 " opportunity, application_quality, eligibility_soft,"
                 " weighted_score, verdict, rationale, model, prompt_version)"
                 " VALUES (1,'scored',90,90,90,90,90,90,'submit',"
                 " 'strong match','m','gate-v1')")
    conn.execute("INSERT INTO job (fingerprint, source, external_id, company,"
                 " company_normalized, title, title_normalized, url)"
                 " VALUES ('fp2','ats','2','Globex','globex','ML Engineer',"
                 " 'engineerml','https://x/2')")
    conn.execute("INSERT INTO assessment (job_id, stage, role_fit, credibility,"
                 " opportunity, application_quality, eligibility_soft,"
                 " weighted_score, verdict, rationale, model, prompt_version)"
                 " VALUES (2,'scored',80,50,80,80,80,72,'skip',"
                 " 'credibility below floor','m','gate-v1')")
    conn.commit()
    return path


@pytest.fixture
def conn(db_path):
    return db.connect(db_path)


@pytest.fixture
def client(db_path, monkeypatch):
    monkeypatch.setattr(web, "DB_PATH", db_path)
    return TestClient(web.app)


def test_conversations_include_home_and_backfilled_job(client, conn):
    conn.execute("INSERT INTO event (job_id, type, payload) VALUES (1, 'human_applied', NULL)")
    conn.commit()
    r = client.get("/api/chat/conversations").json()
    kinds = {c["kind"] for c in r["conversations"]}
    assert kinds == {"home", "job"} and r["home_id"]
    job_conv = next(c for c in r["conversations"] if c["kind"] == "job")
    msgs = client.get(f"/api/chat/{job_conv['id']}/messages").json()["messages"]
    assert any("human_applied" in m["content"] for m in msgs)


def test_post_user_message_and_cursor(client, conn):
    home = client.get("/api/chat/conversations").json()["home_id"]
    mid = client.post(f"/api/chat/{home}/messages", json={"text": "hello"}).json()["message_id"]
    after = client.get(f"/api/chat/{home}/messages?after={mid}").json()["messages"]
    assert all(m["id"] > mid for m in after)


def test_open_prompt_is_surfaced(client, conn):
    pid = chat.open_prompt(conn, 1, "text", {"id": "q1", "question": "Notice period?"})
    cid = chat.conversation_for_job(conn, 1)
    r = client.get(f"/api/chat/{cid}/messages").json()
    assert r["open_prompt"]["id"] == pid and r["open_prompt"]["payload"]["question"] == "Notice period?"
