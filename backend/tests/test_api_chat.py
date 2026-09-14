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
    from career_agent.web import intent
    monkeypatch.setattr(web, "DB_PATH", db_path)
    # A Home message routes through `claude`: never spawn it from a test.
    monkeypatch.setattr(intent, "_default_runner",
                        lambda prompt, schema: {"intent": "help", "job_ref": None, "reply": "hi"})
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
    continuing = c.execute("SELECT id FROM message WHERE content LIKE 'Continuing%'").fetchone()["id"]
    assert r["after"] == continuing                         # the UI waits for a newer line
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


async def test_a_continue_never_re_arms_a_checkpoint_it_no_longer_holds(db_path, runs, monkeypatch):
    """Round 2 Minor 3: the background resume was refused, and meanwhile the
    worker's own session took the checkpoint -- its claim must stand."""
    import asyncio

    c = db.connect(db_path)
    _resumable(c)

    async def fake_tailor(conn, job_id, brief_path):
        return "base-v1"

    async def taken_by_the_worker(conn, job_id, **kw):
        checkpoint.resume(conn, job_id)                     # the worker's session: running
        return {"ok": False, "reason": "job 1 has no resumable checkpoint"}
    monkeypatch.setattr(actions.worker, "tailor_for_apply", fake_tailor)
    monkeypatch.setattr(actions.worker, "guard", lambda *a, **kw: None)
    monkeypatch.setattr(actions.ats_apply, "submit", taken_by_the_worker)
    monkeypatch.setattr(actions, "load_brief", lambda p: "BRIEF")
    monkeypatch.setattr(actions.context, "load_candidate_profile_or_none", lambda p: "PROFILE")
    tasks = set()
    assert actions.resume_job(c, 1, "brief", "profile", None, tasks)["ok"]
    await asyncio.gather(*tasks)
    assert checkpoint.get(c, 1)["auto_resumed"] == 1


# -- Task 20: Home chat commands ----------------------------------------------

import asyncio      # noqa: E402
import json         # noqa: E402
import threading    # noqa: E402

from career_agent.apply import agent as agent_mod   # noqa: E402
from career_agent.web import intent as intent_mod   # noqa: E402
from career_agent.web import worker as worker_mod   # noqa: E402

_EXPIRED = "That request expired — ask again"


def _runner(name, job_ref=None, reply="Sure."):
    return lambda prompt, schema: {"intent": name, "job_ref": job_ref, "reply": reply}


async def _say(conn, text, runner, tmp_path, tasks=None):
    return await actions.home_message(conn, text, _brief(tmp_path), tmp_path / "profile.toml",
                                      None, runner=runner, tasks=tasks)


def _home(conn):
    return chat.messages_after(conn, chat.home_conversation(conn))


def _home_prompt(conn):
    return chat.open_prompt_for_conversation(conn, chat.home_conversation(conn))


def _open_home(conn, action="apply_to", args=None, question="Start?"):
    return chat.open_home_prompt(conn, "approve", {
        "origin": "home", "kind": "approve", "action": action,
        "args": {"job_id": 1} if args is None else args, "question": question})


async def test_home_help_replies_straight_away(conn, tmp_path):
    r = await _say(conn, "what can you do", _runner("help"), tmp_path)
    msgs = _home(conn)
    assert r["ok"] and msgs[-2]["role"] == "user" and msgs[-2]["id"] == r["message_id"]
    assert msgs[-1]["role"] == "agent" and "apply to" in msgs[-1]["content"]
    assert _home_prompt(conn) is None


async def test_home_show_queue_lists_queued_jobs_only(conn, tmp_path):
    await _say(conn, "what's my queue?", _runner("show_queue"), tmp_path)
    reply = _home(conn)[-1]["content"]
    assert "#1 Acme — AI Engineer" in reply and "Globex" not in reply


async def test_home_status_reports_the_run_and_the_queue(conn, tmp_path):
    await _say(conn, "status?", _runner("status"), tmp_path)
    reply = _home(conn)[-1]["content"]
    assert "Apply run: idle" in reply and "1 queued" in reply and "Discovery: idle" in reply


async def test_home_find_jobs_asks_first(conn, tmp_path):
    await _say(conn, "find me jobs", _runner("find_jobs"), tmp_path)
    row = _home_prompt(conn)
    payload = json.loads(row["payload"])
    assert row["kind"] == "approve" and row["job_id"] is None
    assert payload == {"origin": "home", "kind": "approve", "action": "find_jobs", "args": {},
                       "question": "Run discovery now? (uses Apify + scoring credits)"}
    assert worker_mod.get_run_state(conn, "pipeline")["status"] == "idle"   # nothing ran yet


async def test_home_apply_to_one_match_asks_first(conn, tmp_path):
    await _say(conn, "apply to #1", _runner("apply_to", "#1"), tmp_path)
    payload = json.loads(_home_prompt(conn)["payload"])
    assert (payload["action"], payload["args"]) == ("apply_to", {"job_id": 1})
    assert payload["question"] == "Start applying to #1: AI Engineer at Acme?"
    assert _home(conn)[-1]["role"] == "prompt"


async def test_home_apply_to_no_match_replies_without_a_prompt(conn, tmp_path):
    await _say(conn, "apply to Initech", _runner("apply_to", "Initech"), tmp_path)
    assert _home_prompt(conn) is None
    assert "Initech" in _home(conn)[-1]["content"]


async def test_home_apply_to_many_matches_lists_them(conn, tmp_path):
    conn.execute("INSERT INTO job (fingerprint, source, external_id, company,"
                 " company_normalized, title, title_normalized, url)"
                 " VALUES ('fp3','ats','3','Acme','acme','Data Engineer','dataengineer','https://x/3')")
    conn.execute("INSERT INTO assessment (job_id, stage, role_fit, credibility, opportunity,"
                 " application_quality, eligibility_soft, weighted_score, verdict, rationale,"
                 " model, prompt_version) VALUES (3,'scored',90,90,90,90,90,88,'hold','ok','m','gate-v1')")
    conn.commit()
    await _say(conn, "apply to acme", _runner("apply_to", "acme"), tmp_path)
    assert _home_prompt(conn) is None
    reply = _home(conn)[-1]["content"]
    assert "#1" in reply and "#3" in reply


async def test_home_pause_resume_stop_call_the_run_controls(conn, tmp_path, monkeypatch):
    ticks = []

    async def fake_tick(*a, **kw):
        ticks.append(1)
    monkeypatch.setattr(actions.worker, "apply_tick", fake_tick)
    worker_mod.set_run_state(conn, "apply", status="running")
    await _say(conn, "pause", _runner("pause_apply"), tmp_path)
    assert worker_mod.get_run_state(conn, "apply")["status"] == "paused"
    assert "paused" in _home(conn)[-1]["content"]
    tasks = set()
    await _say(conn, "resume", _runner("resume_apply"), tmp_path, tasks)
    await asyncio.gather(*tasks)
    assert worker_mod.get_run_state(conn, "apply")["status"] == "running" and ticks == [1]
    await _say(conn, "stop", _runner("stop_apply"), tmp_path)
    assert worker_mod.get_run_state(conn, "apply")["status"] == "stopped"
    assert _home_prompt(conn) is None


async def test_home_routing_failure_gets_a_helpful_reply(conn, tmp_path):
    def broken(prompt, schema):
        raise RuntimeError("claude CLI not found")
    r = await _say(conn, "apply to #1", broken, tmp_path)
    reply = _home(conn)[-1]
    assert r["ok"] and reply["role"] == "agent" and "claude" in reply["content"]
    assert "help" in reply["content"].lower() and _home_prompt(conn) is None


async def test_home_unknown_intent_suggests_help(conn, tmp_path):
    await _say(conn, "sing", _runner("unknown", reply="I can't sing."), tmp_path)
    reply = _home(conn)[-1]["content"]
    assert reply.startswith("I can't sing.") and "apply to" in reply


async def test_home_routing_does_not_block_the_event_loop(conn, tmp_path):
    started, release = threading.Event(), threading.Event()

    def slow(prompt, schema):
        started.set()
        release.wait(2)
        return {"intent": "help", "job_ref": None, "reply": "hi"}
    task = asyncio.create_task(_say(conn, "help", slow, tmp_path))
    await asyncio.to_thread(started.wait, 2)
    assert started.is_set() and not task.done()     # the loop ran while the runner waits
    release.set()
    assert (await task)["ok"]


def test_home_post_routes_and_surfaces_the_card(client, conn, monkeypatch):
    monkeypatch.setattr(intent_mod, "_default_runner", _runner("apply_to", "#1"))
    home = client.get("/api/chat/conversations").json()["home_id"]
    assert client.post(f"/api/chat/{home}/messages", json={"text": "apply to #1"}).json()["ok"]
    r = client.get(f"/api/chat/{home}/messages").json()
    assert r["open_prompt"]["kind"] == "approve"
    assert r["open_prompt"]["payload"]["question"] == "Start applying to #1: AI Engineer at Acme?"
    assert "Commands arrive" not in str(r["messages"])


def test_a_job_conversation_post_is_unchanged(client, conn, monkeypatch):
    monkeypatch.setattr(intent_mod, "_default_runner", lambda *a: pytest.fail("routed a job chat"))
    cid = chat.conversation_for_job(conn, 1)
    assert client.post(f"/api/chat/{cid}/messages", json={"text": "hi"}).json()["ok"]
    assert [m["role"] for m in chat.messages_after(conn, cid)] == ["user"]


@pytest.fixture
def fake_apply(monkeypatch):
    calls = []

    async def fake(conn, job_id, allow_skip, event, brief_path, candidate_profile_path,
                   conn_factory=None):
        calls.append((job_id, allow_skip, event))
        return {"ok": True, "message": "Applied"}
    monkeypatch.setattr(actions, "do_apply", fake)
    return calls


def _answer(conn, pid, decision, tmp_path, tasks=None):
    return actions.answer_prompt(conn, pid, {"answer": decision}, None,
                                 brief_path=_brief(tmp_path), profile_path=tmp_path / "p.toml",
                                 db_path=tmp_path / "t.db", tasks=tasks,
                                 run_conn_factory="run-factory")


async def test_approving_apply_to_dispatches_do_apply(conn, runs, tmp_path, fake_apply):
    pid = _open_home(conn)
    tasks = set()
    r = _answer(conn, pid, "approve", tmp_path, tasks)
    assert r["ok"] and r["job_id"] == 1
    await asyncio.gather(*tasks)
    assert fake_apply == [(1, False, "human_applied")]
    msgs = _home(conn)
    started = next(m for m in msgs if m["content"].startswith("Started — follow along"))
    assert "#1" in started["content"]
    assert started["payload"] == {"job_id": 1, "conversation_id": chat.conversation_for_job(conn, 1)}
    assert msgs[-1]["content"] == "#1: Applied"
    assert conn.execute("SELECT status FROM agent_prompt WHERE id = ?", (pid,)).fetchone()[0] == "answered"


async def test_approving_apply_to_respects_a_blocking_status(conn, runs, tmp_path, fake_apply):
    conn.execute("INSERT INTO application (job_id, resume_version, status) VALUES (1, 'v', 'held_unknown')")
    conn.commit()
    pid = _open_home(conn)
    tasks = set()
    r = _answer(conn, pid, "approve", tmp_path, tasks)
    assert r["code"] == 409 and "held_unknown" in r["message"]
    assert not tasks and fake_apply == []
    assert "held_unknown" in _home(conn)[-1]["content"]


async def test_approving_apply_to_a_vanished_job_refuses(conn, runs, tmp_path, fake_apply):
    pid = _open_home(conn, args={"job_id": 999})
    r = _answer(conn, pid, "approve", tmp_path)
    assert r["code"] == 409 and "#999" in r["message"] and fake_apply == []


async def test_approving_find_jobs_uses_run_now(conn, runs, tmp_path, monkeypatch):
    ran = []

    async def fake_background(*a, **kw):
        ran.append(a)
    monkeypatch.setattr(actions.pipeline, "run_background", fake_background)
    pid = _open_home(conn, action="find_jobs", args={})
    tasks = set()
    assert _answer(conn, pid, "approve", tmp_path, tasks)["ok"]
    await asyncio.gather(*tasks)
    assert ran[0][0] == "run-factory"            # M4: Run Now's own factory
    assert worker_mod.get_run_state(conn, "pipeline")["status"] == "running"
    assert "Discovery started" in _home(conn)[-1]["content"]
    again = _open_home(conn, action="find_jobs", args={})
    r = _answer(conn, again, "approve", tmp_path, set())
    assert r["code"] == 409 and "already in progress" in _home(conn)[-1]["content"]


async def test_rejecting_a_home_prompt_cancels(conn, runs, tmp_path, fake_apply):
    pid = _open_home(conn)
    tasks = set()
    r = _answer(conn, pid, "reject", tmp_path, tasks)
    assert r["ok"] and not tasks and fake_apply == []
    assert _home(conn)[-1]["content"] == "Cancelled"
    assert conn.execute("SELECT status FROM agent_prompt WHERE id = ?", (pid,)).fetchone()[0] == "answered"


async def test_a_stale_home_prompt_is_expired(conn, runs, tmp_path, fake_apply):
    pid = _open_home(conn)
    conn.execute("UPDATE agent_prompt SET created_at = datetime('now', '-11 minutes') WHERE id = ?", (pid,))
    conn.commit()
    r = _answer(conn, pid, "approve", tmp_path)
    assert (r["code"], r["message"]) == (409, _EXPIRED) and fake_apply == []
    assert conn.execute("SELECT status FROM agent_prompt WHERE id = ?", (pid,)).fetchone()[0] == "expired"


async def test_a_superseded_home_prompt_is_expired(conn, runs, tmp_path, fake_apply):
    older = _open_home(conn)
    newer = _open_home(conn, action="find_jobs", args={})
    r = _answer(conn, older, "approve", tmp_path)
    assert (r["code"], r["message"]) == (409, _EXPIRED) and fake_apply == []
    assert _home_prompt(conn)["id"] == newer


async def test_a_non_allowlisted_home_action_is_422(conn, runs, tmp_path, fake_apply):
    pid = _open_home(conn, action="run_stop", args={})
    r = _answer(conn, pid, "approve", tmp_path)
    assert r["code"] == 422 and fake_apply == []
    assert worker_mod.get_run_state(conn, "apply")["status"] == "idle"


async def test_an_agent_ask_cannot_spoof_a_home_action(conn, runs, tmp_path, fake_apply):
    line = agent_mod.ask_prefix("n0nce") + json.dumps({
        "id": "x", "kind": "approve", "question": "Approve?", "origin": "home",
        "action": "apply_to", "args": {"job_id": 1}})
    parsed = agent_mod.parse_ask(line, "n0nce")
    assert parsed and "origin" not in parsed
    # Even with origin intact, an agent card lives in the job's conversation.
    parsed["origin"] = "home"
    pid = chat.open_prompt(conn, 1, "approve", parsed)
    tasks = set()
    r = _answer(conn, pid, "approve", tmp_path, tasks)
    assert (r["code"], r["message"]) == (409, "No live agent run for this job")
    assert not tasks and fake_apply == [] and _home(conn) == []


def test_answering_a_home_card_over_http(client, conn, runs, monkeypatch, tmp_path):
    monkeypatch.setattr(web, "BRIEF_PATH", _brief(tmp_path))
    pid = _open_home(conn)
    r = client.post(f"/api/chat/prompts/{pid}/answer", json={"answer": "reject"})
    assert r.status_code == 200 and r.json()["message"] == "Cancelled"


def _old_prompt_table(c):
    c.executescript("DROP TABLE agent_prompt; CREATE TABLE agent_prompt (id INTEGER PRIMARY KEY,"
                    " job_id INTEGER NOT NULL REFERENCES job(id), conversation_id INTEGER NOT NULL"
                    " REFERENCES conversation(id), kind TEXT NOT NULL, payload TEXT NOT NULL,"
                    " status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered','expired')),"
                    " answer TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')), answered_at TEXT);")


def _notnull(c):
    return {r["name"]: r["notnull"] for r in c.execute("PRAGMA table_info(agent_prompt)")}


def test_an_old_db_gets_a_nullable_prompt_job_id(tmp_path):
    """I2: rows survive, an orphan included, and nothing is left mid-transaction."""
    c = db.connect(tmp_path / "old.db")
    db.init_schema(c)
    _old_prompt_table(c)
    c.execute("PRAGMA foreign_keys = OFF")
    c.execute("INSERT INTO agent_prompt (id, job_id, conversation_id, kind, payload)"
              " VALUES (5, 77, 88, 'text', '{\"question\": \"Q?\"}')")      # orphan: no job 77
    c.commit()
    c.execute("PRAGMA foreign_keys = ON")
    db.init_schema(c)
    db.init_schema(c)
    assert _notnull(c)["job_id"] == 0 and _notnull(c)["conversation_id"] == 1
    assert [tuple(r) for r in c.execute("SELECT id, job_id, conversation_id, kind FROM agent_prompt")] \
        == [(5, 77, 88, "text")]
    assert not c.in_transaction and c.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_a_failed_prompt_migration_leaves_the_connection_usable(tmp_path):
    import sqlite3

    c = db.connect(tmp_path / "old.db")
    db.init_schema(c)
    _old_prompt_table(c)
    c.execute("CREATE TABLE agent_prompt_new (x)")     # makes the rebuild's CREATE fail
    c.commit()
    with pytest.raises(sqlite3.OperationalError):
        db.init_schema(c)
    assert not c.in_transaction and c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert _notnull(c)["job_id"] == 1                  # rolled back, untouched
    c.execute("INSERT INTO event (job_id, type) VALUES (NULL, 'still_writable')")
    c.commit()


# -- Task 20 fix round 1 ------------------------------------------------------

async def test_home_never_starts_a_run_from_free_text(conn, tmp_path, monkeypatch):
    """I1."""
    monkeypatch.setattr(actions.worker, "apply_tick", lambda *a, **kw: pytest.fail("ticked"))
    for status in ("idle", "stopped"):
        worker_mod.set_run_state(conn, "apply", status=status)
        tasks = set()
        await _say(conn, "continue", _runner("resume_apply"), tmp_path, tasks)
        assert not tasks and worker_mod.get_run_state(conn, "apply")["status"] == status
        assert _home(conn)[-1]["content"] == f"Nothing to resume — the apply run is {status}."
    worker_mod.set_run_state(conn, "apply", status="idle")
    await _say(conn, "pause", _runner("pause_apply"), tmp_path)
    assert worker_mod.get_run_state(conn, "apply")["status"] == "idle"
    assert _home(conn)[-1]["content"] == "Nothing to pause — the apply run is idle."


async def test_home_resume_failure_is_reported_without_details(conn, tmp_path, monkeypatch):
    """M7 + M2."""
    async def boom(*a, **kw):
        raise RuntimeError("secret C:/path")
    monkeypatch.setattr(actions.worker, "apply_tick", boom)
    worker_mod.set_run_state(conn, "apply", status="paused")
    tasks = set()
    await _say(conn, "resume", _runner("resume_apply"), tmp_path, tasks)
    await asyncio.gather(*tasks)
    last = _home(conn)[-1]["content"]
    assert "failed" in last and "secret" not in last


async def test_home_errors_never_echo_exception_text(conn, tmp_path, monkeypatch, runs):
    """M2."""
    def boom(conn):
        raise RuntimeError("secret C:/path")
    monkeypatch.setattr(actions, "_queue_text", boom)
    await _say(conn, "queue?", _runner("show_queue"), tmp_path)
    assert "secret" not in _home(conn)[-1]["content"]

    async def failing_apply(*a, **kw):
        raise RuntimeError("secret C:/path")
    monkeypatch.setattr(actions, "do_apply", failing_apply)
    tasks = set()
    assert _answer(conn, _open_home(conn), "approve", tmp_path, tasks)["ok"]
    await asyncio.gather(*tasks)
    last = _home(conn)[-1]["content"]
    assert last.startswith("#1: Apply failed") and "secret" not in last


async def test_a_genuine_unknown_is_not_reported_as_router_down(conn, tmp_path):
    """M1."""
    await _say(conn, "hmm", _runner("unknown", reply="Sorry, I didn't understand that. Try 'help'."),
               tmp_path)
    assert "router" not in _home(conn)[-1]["content"]


async def test_the_apply_question_leads_with_the_id_and_clips_scraped_text(conn, tmp_path):
    """M3."""
    conn.execute("UPDATE job SET title = ? WHERE id = 1", ("Engineer (#12) " + "x" * 200,))
    conn.commit()
    await _say(conn, "apply to #1", _runner("apply_to", "#1"), tmp_path)
    question = json.loads(_home_prompt(conn)["payload"])["question"]
    assert question.startswith("Start applying to #1: Engineer (#12)") and len(question) < 130


async def test_a_gate_skipped_job_points_home_at_the_override(conn, runs, tmp_path, fake_apply):
    """M5: job 2's verdict is skip."""
    r = _answer(conn, _open_home(conn, args={"job_id": 2}), "approve", tmp_path)
    last = _home(conn)[-1]["content"]
    assert r["code"] == 409 and "Jobs panel" in last and "Apply anyway" not in last
    assert fake_apply == []


def test_listing_home_messages_expires_a_stale_card(client, conn):
    """M6."""
    pid = _open_home(conn)
    conn.execute("UPDATE agent_prompt SET created_at = datetime('now', '-11 minutes') WHERE id = ?", (pid,))
    conn.commit()
    r = client.get(f"/api/chat/{chat.home_conversation(conn)}/messages").json()
    assert r["open_prompt"] is None
    assert conn.execute("SELECT status FROM agent_prompt WHERE id = ?", (pid,)).fetchone()[0] == "expired"


def test_a_crafted_answer_body_cannot_change_the_action(client, conn, runs, monkeypatch, tmp_path,
                                                        fake_apply):
    monkeypatch.setattr(web, "BRIEF_PATH", _brief(tmp_path))
    pid = _open_home(conn)
    r = client.post(f"/api/chat/prompts/{pid}/answer",
                    json={"answer": "approve", "action": "find_jobs", "args": {"job_id": 2}})
    assert r.status_code == 200 and r.json()["job_id"] == 1
    assert worker_mod.get_run_state(conn, "pipeline")["status"] == "idle"


async def test_a_home_row_without_origin_does_not_dispatch(conn, runs, tmp_path, fake_apply):
    pid = chat.open_home_prompt(conn, "approve", {"kind": "approve", "action": "apply_to",
                                                  "args": {"job_id": 1}, "question": "Start?"})
    tasks = set()
    r = _answer(conn, pid, "approve", tmp_path, tasks)
    assert r["code"] == 409 and not tasks and fake_apply == []


async def test_home_apply_to_is_refused_by_pause_or_the_daily_cap(conn, runs, tmp_path, fake_apply):
    conn.execute("INSERT INTO event (job_id, type, payload) VALUES (NULL, 'pause', 'on')")
    conn.commit()
    r = _answer(conn, _open_home(conn), "approve", tmp_path)
    assert r["code"] == 409 and "paused" in r["message"]
    conn.execute("INSERT INTO event (job_id, type, payload) VALUES (NULL, 'pause', 'off')")
    conn.execute("INSERT INTO application (job_id, resume_version, status, submitted_at)"
                 " VALUES (2, 'v', 'submitted', datetime('now'))")
    conn.commit()
    capped = tmp_path / "cap.toml"
    capped.write_text('target_titles = ["AI Engineer"]\nsearch_locations = ["Chennai"]\ndaily_cap = 1\n')
    r = actions.answer_prompt(conn, _open_home(conn), {"answer": "approve"}, None, brief_path=capped,
                              profile_path=tmp_path / "p.toml", db_path=tmp_path / "t.db", tasks=set())
    assert r["code"] == 409 and "Daily cap" in r["message"] and fake_apply == []


def test_a_job_card_answer_runs_off_the_event_loop(client, conn, runs, monkeypatch):
    """I3: Task 15's sync Playwright fill can't run inside a running loop."""
    seen = []

    def fake(c, prompt_id, answer, conn_factory=None, **kw):
        try:
            asyncio.get_running_loop()
            seen.append("on loop")
        except RuntimeError:
            seen.append("off loop")
        return {"ok": True, "message": "Answer sent"}
    monkeypatch.setattr(actions, "answer_prompt", fake)
    pid = _confirm_prompt(conn)
    assert client.post(f"/api/chat/prompts/{pid}/answer", json={"decision": "approve"}).status_code == 200
    assert seen == ["off loop"]
