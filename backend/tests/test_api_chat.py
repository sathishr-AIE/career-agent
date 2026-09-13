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


def test_post_to_unknown_conversation_is_404(client, conn):
    r = client.post("/api/chat/999/messages", json={"text": "hello"})
    assert r.status_code == 404


def test_post_blank_message_is_422(client, conn):
    home = client.get("/api/chat/conversations").json()["home_id"]
    r = client.post(f"/api/chat/{home}/messages", json={"text": "   "})
    assert r.status_code == 422


def test_second_conversations_poll_does_no_backfill(client, conn):
    conn.execute("INSERT INTO event (job_id, type, payload) VALUES (1, 'human_applied', NULL)")
    conn.commit()
    assert chat.jobs_needing_backfill(conn) == [1]
    client.get("/api/chat/conversations")
    assert chat.jobs_needing_backfill(conn) == []
    before = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
    client.get("/api/chat/conversations")
    assert conn.execute("SELECT COUNT(*) FROM message").fetchone()[0] == before


class _LiveRun:
    """A registered run waiting on its prompt: same send() contract as AgentRun."""
    def __init__(self):
        import threading
        from career_agent.apply.runner import RunEvents
        self.events = RunEvents()
        self.nonce, self.sent = "n0nce", []
        self.waiting, self.done = threading.Event(), threading.Event()
        self.waiting.set()

    def send(self, text):
        if self.done.is_set() or not self.waiting.is_set():
            return False
        self.waiting.clear()
        self.sent.append(text)
        return True


@pytest.fixture
def runs(monkeypatch):
    from career_agent.apply import agent as agent_mod
    d = {}
    monkeypatch.setattr(agent_mod, "RUNS", d)
    return d


def _confirm_prompt(conn):
    return chat.open_prompt(conn, 1, "confirm", {"fields": [{"label": "Name", "value": "Asha"}]})


def test_answer_a_prompt_relays_it_to_the_live_run(client, conn, runs):
    run = runs[1] = _LiveRun()
    pid = _confirm_prompt(conn)
    r = client.post(f"/api/chat/prompts/{pid}/answer", json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["ok"]
    assert run.sent == ['DECISION:n0nce:{"decision": "approve"}']
    row = conn.execute("SELECT status, answer FROM agent_prompt WHERE id = ?", (pid,)).fetchone()
    assert row["status"] == "answered"


def test_answer_with_no_live_run_is_409_and_stays_open(client, conn, runs):
    pid = _confirm_prompt(conn)
    r = client.post(f"/api/chat/prompts/{pid}/answer", json={"decision": "approve"})
    assert r.status_code == 409
    assert r.json() == {"ok": False, "code": 409, "message": "No live agent run for this job"}
    assert chat.open_prompt_for_job(conn, 1)["id"] == pid


def test_answering_twice_is_409_no_longer_open(client, conn, runs):
    runs[1] = _LiveRun()
    pid = _confirm_prompt(conn)
    assert client.post(f"/api/chat/prompts/{pid}/answer", json={"decision": "cancel"}).status_code == 200
    r = client.post(f"/api/chat/prompts/{pid}/answer", json={"decision": "cancel"})
    assert r.status_code == 409 and r.json()["message"] == "That question is no longer open"


def test_an_invalid_answer_is_422(client, conn, runs):
    runs[1] = _LiveRun()
    pid = _confirm_prompt(conn)
    r = client.post(f"/api/chat/prompts/{pid}/answer", json={"decision": "yolo"})
    assert r.status_code == 422 and not r.json()["ok"]
    assert runs[1].sent == []


def test_an_unknown_prompt_is_404(client, conn, runs):
    assert client.post("/api/chat/prompts/999/answer", json={"decision": "approve"}).status_code == 404
