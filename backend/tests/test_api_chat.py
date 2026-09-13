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


# -- Task 9 --------------------------------------------------------------------

def _needs_answer_prompt(conn, question="Notice period?"):
    return chat.open_prompt(conn, 1, "text", {
        "id": "needs_answer", "kind": "text", "question": question,
        "why": "The agent stopped to ask this before continuing.", "origin": "needs_answer",
        "memory_key": None, "default": None, "options": [], "sensitive": False})


def test_a_needs_answer_card_is_answered_without_a_live_run(client, conn, runs):
    from career_agent import store
    from career_agent.web import worker
    worker.set_run_state(conn, "apply", status="running", mode="manual", current_job_id=1)
    pid = _needs_answer_prompt(conn)
    r = client.post(f"/api/chat/prompts/{pid}/answer", json={"answer": " 30 days "})
    assert r.status_code == 200 and r.json()["ok"]
    assert store.qa_lookup(conn, "Notice period?")["answer"] == "30 days"
    assert worker.get_run_state(conn, "apply")["current_job_id"] is None
    status = conn.execute("SELECT status FROM agent_prompt WHERE id = ?", (pid,)).fetchone()[0]
    assert status == "answered"
    types = [e[0] for e in conn.execute("SELECT type FROM event WHERE job_id = 1")]
    assert "needs_answer_resolved" in types
    msgs = chat.messages_after(conn, chat.conversation_for_job(conn, 1))
    assert msgs[-1]["role"] == "user" and msgs[-1]["content"] == "Notice period? → 30 days"


def test_a_blank_needs_answer_is_422(client, conn, runs):
    pid = _needs_answer_prompt(conn)
    assert client.post(f"/api/chat/prompts/{pid}/answer", json={"answer": "  "}).status_code == 422
    assert chat.open_prompt_for_job(conn, 1)["id"] == pid


def test_a_normal_ask_card_still_needs_a_live_run(client, conn, runs):
    pid = chat.open_prompt(conn, 1, "text", {"id": "q1", "kind": "text", "question": "Notice?"})
    r = client.post(f"/api/chat/prompts/{pid}/answer", json={"answer": "30 days"})
    assert r.status_code == 409 and r.json()["message"] == "No live agent run for this job"


def test_a_change_with_a_blank_value_is_422(client, conn, runs):
    runs[1] = _LiveRun()
    pid = _confirm_prompt(conn)
    r = client.post(f"/api/chat/prompts/{pid}/answer",
                    json={"decision": "change", "changes": {"Name": "  "}})
    assert r.status_code == 422 and runs[1].sent == []
    assert chat.open_prompt_for_job(conn, 1)["id"] == pid


def test_messages_carry_the_prompt_status(client, conn, runs):
    answered = chat.open_prompt(conn, 1, "text", {"id": "a", "question": "A?"})
    chat.answer_prompt_row(conn, answered, {"answer": "x"})
    expired = chat.open_prompt(conn, 1, "text", {"id": "b", "question": "B?"})
    chat.expire_open_prompts(conn, 1)
    still_open = chat.open_prompt(conn, 1, "text", {"id": "c", "question": "C?"})
    cid = chat.conversation_for_job(conn, 1)
    chat.post_message(conn, cid, "system", "hi")
    msgs = client.get(f"/api/chat/{cid}/messages").json()["messages"]
    by_pid = {m["payload"]["prompt_id"]: m["prompt_status"] for m in msgs if m["role"] == "prompt"}
    assert by_pid == {answered: "answered", expired: "expired", still_open: "open"}
    assert all("prompt_status" not in m for m in msgs if m["role"] != "prompt")


def test_a_confirm_card_message_reads_review_before_applying(conn):
    _confirm_prompt(conn)
    msgs = chat.messages_after(conn, chat.conversation_for_job(conn, 1))
    assert msgs[-1]["content"] == "Review before applying"


def test_job_conversation_lookup(client, conn):
    r = client.get("/api/chat/jobs/1/conversation")
    assert r.status_code == 200 and r.json()["id"] == chat.conversation_for_job(conn, 1)


# -- Task 11: Continue where it left off ----------------------------------------

from career_agent.apply import ats as ats_apply        # noqa: E402
from career_agent.apply import checkpoint              # noqa: E402
from career_agent.web import actions                   # noqa: E402


def _resumable(conn, job_id=1):
    checkpoint.start(conn, job_id, "sess", "n0nce", mode="manual", can_submit=False)
    checkpoint.mark_resumable(conn, job_id)


def test_messages_report_resumable(client, conn):
    cid = chat.conversation_for_job(conn, 1)
    assert client.get(f"/api/chat/{cid}/messages").json()["resumable"] is False
    _resumable(conn)
    assert client.get(f"/api/chat/{cid}/messages").json()["resumable"] is True
    home = client.get("/api/chat/conversations").json()["home_id"]
    assert client.get(f"/api/chat/{home}/messages").json()["resumable"] is False


def test_resume_refuses_without_a_resumable_checkpoint(client, conn, runs):
    r = client.post("/api/chat/jobs/1/resume")
    assert r.status_code == 409 and not r.json()["ok"] and r.json()["message"]
    checkpoint.start(conn, 1, "s", "n")                    # running, not resumable
    assert client.post("/api/chat/jobs/1/resume").status_code == 409


def test_resume_refuses_a_blocking_application(client, conn, runs):
    _resumable(conn)
    conn.execute("INSERT INTO application (job_id, resume_version, status) VALUES (1, 'v', 'held_unknown')")
    conn.commit()
    r = client.post("/api/chat/jobs/1/resume")
    assert r.status_code == 409 and "held_unknown" in r.json()["message"]


def test_resume_refuses_a_live_run(client, conn, runs):
    _resumable(conn)
    runs[1] = _LiveRun()
    r = client.post("/api/chat/jobs/1/resume")
    assert r.status_code == 409 and "live" in r.json()["message"]


async def test_resume_refuses_while_another_run_holds_the_lock(db_path, runs):
    c = db.connect(db_path)
    _resumable(c)
    async with ats_apply._agent_lock():
        r = actions.resume_job(c, 1, "brief", "profile", None, set())
    assert (r["ok"], r["code"]) == (False, 409)


async def test_resume_launches_a_resumed_submit_in_the_background(db_path, runs, monkeypatch):
    import asyncio

    c = db.connect(db_path)
    _resumable(c)
    checkpoint.set_auto_resumed(c, 1, True)
    calls = []

    async def fake_tailor(conn, job_id, brief_path):
        return "base-v1"

    async def fake_submit(conn, job_id, **kw):
        calls.append((job_id, kw, checkpoint.get(conn, job_id)["auto_resumed"]))
        return {"ok": True, "job_id": job_id, "status": "draft"}
    monkeypatch.setattr(actions.worker, "tailor_for_apply", fake_tailor)
    monkeypatch.setattr(actions.worker, "guard", lambda *a, **kw: None)
    monkeypatch.setattr(actions.ats_apply, "submit", fake_submit)
    monkeypatch.setattr(actions, "load_brief", lambda p: "BRIEF")
    monkeypatch.setattr(actions.context, "load_candidate_profile_or_none", lambda p: "PROFILE")
    tasks = set()
    r = actions.resume_job(c, 1, "brief", "profile", "factory", tasks)
    assert r["ok"] and r["message"]
    await asyncio.gather(*tasks)
    job_id, kw, claimed = calls[0]
    assert claimed == 1                                         # I5b: claimed while it runs
    assert job_id == 1 and kw["resume"] is True and kw["mode"] == "manual"
    assert kw["conn_factory"] == "factory" and kw["resume_version"] == "base-v1"
    assert checkpoint.get(c, 1)["auto_resumed"] == 0            # a human touch re-arms auto-resume
    texts = [m["content"] for m in chat.messages_after(c, chat.conversation_for_job(c, 1), 0)]
    assert any("Continuing" in t for t in texts)


def test_resume_endpoint_succeeds(client, conn, runs, monkeypatch, tmp_path):
    _resumable(conn)
    monkeypatch.setattr(web, "BRIEF_PATH", _brief(tmp_path))
    monkeypatch.setattr(actions, "_resume_run", lambda *a, **kw: _noop())
    r = client.post("/api/chat/jobs/1/resume")
    assert r.status_code == 200 and r.json()["ok"]


async def _noop():
    return None


# -- Task 11 fix round 1 ------------------------------------------------------

def _brief(tmp_path):
    p = tmp_path / "career_brief.toml"
    p.write_text('target_titles = ["AI Engineer"]\nsearch_locations = ["Chennai"]\ndaily_cap = 5\n')
    return p


def test_resume_refuses_while_the_apply_run_is_paused(client, conn, runs, monkeypatch, tmp_path):
    """I4: Continue goes through the same guard as Apply."""
    _resumable(conn)
    monkeypatch.setattr(web, "BRIEF_PATH", _brief(tmp_path))
    conn.execute("INSERT INTO event (job_id, type, payload) VALUES (NULL, 'pause', 'on')")
    conn.commit()
    r = client.post("/api/chat/jobs/1/resume")
    assert r.status_code == 409 and "paused" in r.json()["message"]
    assert checkpoint.get(conn, 1)["auto_resumed"] == 0         # a refusal claims nothing


async def test_resume_refuses_while_another_job_is_parked(db_path, runs):
    from career_agent.web import worker as wk

    c = db.connect(db_path)
    _resumable(c)
    wk.set_run_state(c, "apply", current_job_id=2)
    r = actions.resume_job(c, 1, "brief", "profile", None, set())
    assert r["code"] == 409 and "parked" in r["message"]


def test_a_blocked_job_is_not_offered_continue(client, conn):
    """M1."""
    _resumable(conn)
    conn.execute("INSERT INTO application (job_id, resume_version, status) VALUES (1, 'v', 'held_unknown')")
    conn.commit()
    cid = chat.conversation_for_job(conn, 1)
    assert client.get(f"/api/chat/{cid}/messages").json()["resumable"] is False


async def test_lifespan_sweeps_before_the_first_conn(db_path, monkeypatch):
    """I2: startup_sweep must run before any _conn() (its stale sweep holds first)."""
    import asyncio

    order = []
    real_conn = web._conn
    monkeypatch.setattr(web, "DB_PATH", db_path)
    monkeypatch.setattr(web.worker, "startup_sweep", lambda c: order.append("startup_sweep"))
    monkeypatch.setattr(web, "_conn", lambda: (order.append("_conn"), real_conn())[1])

    async def idle(*a, **kw):
        await asyncio.sleep(3600)
    monkeypatch.setattr(web.worker, "apply_worker_loop", idle)
    async with web.lifespan(web.app):
        pass
    await asyncio.sleep(0)
    assert order[:2] == ["startup_sweep", "_conn"]
