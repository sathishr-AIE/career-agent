import asyncio
import json
import logging
from pathlib import Path

import docx
import pytest

from career_agent import db, store
from career_agent.apply import agent as agent_mod
from career_agent.apply import ats as ats_apply
from career_agent.apply.agent import AgentResult
from career_agent.config import CandidateProfile, CareerBrief


@pytest.fixture(autouse=True)
def work_dir(tmp_path, monkeypatch):
    """submit() stages a clean copy of the résumé in the agent's work dir
    before handing the path to the prompt. Keep every test's copy inside
    its own tmp_path rather than scattering files in the real temp dir."""
    d = tmp_path / "work"
    monkeypatch.setattr(agent_mod, "WORK_DIR", d)
    return d


@pytest.fixture
def conn(tmp_path):
    # A real file on disk, but not a readable .docx: submit() refuses when
    # the résumé the agent is told to upload does not exist, while
    # _resume_text still has to degrade gracefully on one it can't parse.
    resume = tmp_path / "r.docx"
    resume.write_text("not a real docx", encoding="utf-8")
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    c.execute("INSERT INTO job (fingerprint, source, external_id, company,"
              " company_normalized, title, title_normalized, url)"
              " VALUES ('fp','ats','1','Acme','acme','AI Engineer',"
              " 'aiengineer','https://x/apply')")
    c.execute("INSERT INTO resume (version, path) VALUES ('base-v1', ?)",
              (str(resume),))
    c.commit()
    return c


PROFILE = CandidateProfile(candidate_name="Jane Doe",
                           candidate_email="jane@example.com",
                           candidate_phone="+91-90000-00000")

BRIEF = CareerBrief(target_titles=["AI Engineer"],
                    search_locations=["Chennai"],
                    locations=["Chennai", "Bengaluru"])


def fake_agent(result: AgentResult):
    """The one test seam. Nothing in this file may launch Chrome, spawn a
    subprocess, or need the `claude`/`npx` binaries -- every case injects
    this instead of letting submit() reach _live_run_agent."""
    async def _fake(prompt, job_id, nonce):
        _fake.prompts.append(prompt)
        _fake.job_ids.append(job_id)
        _fake.nonces.append(nonce)
        return result
    _fake.prompts = []
    _fake.job_ids = []
    _fake.nonces = []
    return _fake


async def _submit(conn, **kw):
    kw.setdefault("brief", BRIEF)
    kw.setdefault("profile", PROFILE)
    return await ats_apply.submit(conn, 1, **kw)


def _apps(conn):
    return conn.execute("SELECT * FROM application WHERE job_id = 1"
                        " ORDER BY id").fetchall()


def _event_types(conn):
    return [r["type"] for r in conn.execute("SELECT type FROM event")]


# The four "the agent stopped reporting mid-run" reasons: unknown state on
# the send path (held_unknown), plain retryable failures on the draft path.
UNKNOWN_STATE = ("agent_error", "timeout", "no_result_line",
                 "unrecognized_result:BLAH")


# -- pure helpers ---------------------------------------------------------

def test_classify_failure():
    assert ats_apply.classify_failure("sso_required", 0) == "failed_permanent"
    assert ats_apply.classify_failure("easy_apply", 0) == "failed_permanent"
    assert ats_apply.classify_failure("stuck", 0) == "failed"
    assert ats_apply.classify_failure(
        "stuck", ats_apply.MAX_ATTEMPTS - 1) == "failed_permanent"


def test_is_unknown_state():
    for reason in UNKNOWN_STATE:
        assert ats_apply.is_unknown_state(reason), reason
    for reason in ("stuck", "page_error", "sso_required", "expired",
                   "login_issue", ""):
        assert not ats_apply.is_unknown_state(reason), reason


def test_confirmed_within_days_boundary(conn):
    """Kept from the resolve_answers era: the naive-UTC window agent.py's
    prompt builder uses to mark a volatile answer stale."""
    store.qa_upsert(conn, "Current CTC?", "12 LPA", is_volatile=True)
    conn.execute("UPDATE qa_bank SET last_confirmed_at ="
                 " datetime('now', '-29 days')")
    conn.commit()
    row = store.qa_lookup(conn, "Current CTC?")
    assert ats_apply._confirmed_within_days(row["last_confirmed_at"], 30)
    conn.execute("UPDATE qa_bank SET last_confirmed_at ="
                 " datetime('now', '-31 days')")
    conn.commit()
    row = store.qa_lookup(conn, "Current CTC?")
    assert not ats_apply._confirmed_within_days(row["last_confirmed_at"], 30)


# -- draft (dry_run=True) -------------------------------------------------

async def test_draft_inserts_draft_row_with_answers(conn):
    fake = fake_agent(AgentResult("draft_ready", answers={"Visa?": "Citizen"},
                                  transcript_path="t.txt"))
    r = await _submit(conn, dry_run=True, run_agent=fake)
    assert r["ok"] and r["status"] == "draft"
    row = _apps(conn)[0]
    assert row["status"] == "draft"
    assert json.loads(row["answers"]) == {"Visa?": "Citizen"}
    assert row["transcript_path"] == "t.txt"
    assert row["submitted_at"] is None
    assert fake.job_ids == [1]


async def test_draft_prompt_is_draft_mode_and_carries_qa_bank(conn):
    store.qa_upsert(conn, "Notice period?", "30 days", is_volatile=False)
    fake = fake_agent(AgentResult("draft_ready", answers={}))
    await _submit(conn, dry_run=True, run_agent=fake)
    prompt = fake.prompts[0]
    assert "Do NOT click" in prompt          # draft-mode ending
    assert "PINNED ANSWERS" not in prompt
    assert "notice period -> 30 days" in prompt


async def test_draft_needs_answer_passthrough(conn):
    fake = fake_agent(AgentResult("needs_answer", "Do you have a PMP?"))
    r = await _submit(conn, dry_run=True, run_agent=fake)
    assert not r["ok"] and r["needs_answer"] == "Do you have a PMP?"
    assert _apps(conn) == []


async def test_draft_captcha_holds_without_application_row(conn):
    fake = fake_agent(AgentResult("captcha"))
    r = await _submit(conn, dry_run=True, run_agent=fake)
    assert not r["ok"] and r["held"]
    assert _apps(conn) == []
    assert "captcha_held" in _event_types(conn)


async def test_expired_at_draft_time_is_permanent(conn):
    fake = fake_agent(AgentResult("expired"))
    r = await _submit(conn, dry_run=True, run_agent=fake)
    assert not r["ok"]
    row = _apps(conn)[0]
    assert row["status"] == "failed_permanent"
    assert row["failure_reason"] == "expired"
    assert "failed_permanent" in _event_types(conn)


async def test_draft_failure_records_a_failed_row(conn):
    fake = fake_agent(AgentResult("failed", "stuck"))
    r = await _submit(conn, dry_run=True, run_agent=fake)
    assert not r["ok"]
    row = _apps(conn)[0]
    assert row["status"] == "failed"
    assert row["failure_reason"] == "stuck"


async def test_draft_that_claims_it_applied_is_held_not_retried(conn):
    """Draft mode forbids clicking Submit. An APPLIED there means the true
    state is unknown and possibly submitted -- it must block, never become
    a retryable failure that sends a second time."""
    fake = fake_agent(AgentResult("applied", answers={"q": "a"}))
    r = await _submit(conn, dry_run=True, run_agent=fake)
    assert not r["ok"]
    row = _apps(conn)[0]
    assert row["status"] == "held_unknown"
    assert row["failure_reason"] == "applied_during_draft"


# -- send (dry_run=False) -------------------------------------------------

async def test_send_pins_draft_answers_into_prompt(conn):
    await _submit(conn, dry_run=True,
                  run_agent=fake_agent(AgentResult("draft_ready",
                                                   answers={"Visa?": "Citizen"})))
    fake = fake_agent(AgentResult("applied", answers={"Visa?": "Citizen"},
                                  transcript_path="s.txt"))
    r = await _submit(conn, dry_run=False, run_agent=fake)
    assert r["ok"] and r["status"] == "submitted"
    assert "PINNED ANSWERS" in fake.prompts[0]
    assert "Visa? -> Citizen" in fake.prompts[0]   # the value is the invariant
    row = _apps(conn)[-1]
    assert row["status"] == "submitted"
    assert row["submitted_at"] is not None
    assert row["transcript_path"] == "s.txt"
    assert json.loads(row["answers"]) == {"Visa?": "Citizen"}
    assert "submitted" in _event_types(conn)


@pytest.mark.parametrize("reported", [None, {}])
async def test_send_uses_pinned_answers_when_the_agent_reports_none(
        conn, reported):
    """The review invariant: what a human reviewed is what gets recorded.
    An empty ANSWERS_JSON is 'nothing reported', not 'no answers given'."""
    await _submit(conn, dry_run=True,
                  run_agent=fake_agent(AgentResult("draft_ready",
                                                   answers={"Visa?": "Citizen"})))
    await _submit(conn, dry_run=False,
                  run_agent=fake_agent(AgentResult("applied", answers=reported)))
    row = _apps(conn)[-1]
    assert json.loads(row["answers"]) == {"Visa?": "Citizen"}


async def test_a_draft_with_no_answers_still_sends_in_send_mode(conn):
    """A draft on record means a human review is expected -- falling
    through to auto mode would let the agent improvise and submit."""
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'base-v1', 'draft')")
    conn.commit()
    fake = fake_agent(AgentResult("applied"))
    await _submit(conn, dry_run=False, run_agent=fake)
    assert "PINNED ANSWERS" in fake.prompts[0]


@pytest.mark.parametrize("result,expected", [
    (AgentResult("login_issue"), "failed"),
    (AgentResult("draft_ready", answers={"q": "a"}), "failed"),
])
async def test_send_dispatch_for_a_non_submitting_outcome(conn, result,
                                                          expected):
    """Neither clicked Submit, and both are known states -- retryable."""
    r = await _submit(conn, dry_run=False, run_agent=fake_agent(result))
    assert not r["ok"]
    row = _apps(conn)[-1]
    assert row["status"] == expected
    assert row["failure_reason"] == (result.reason or result.code)


async def test_send_without_draft_runs_auto_mode(conn):
    fake = fake_agent(AgentResult("applied", answers={"q": "a"}))
    r = await _submit(conn, dry_run=False, run_agent=fake)
    assert r["ok"]
    assert "PINNED ANSWERS" not in fake.prompts[0]   # auto mode, nothing pinned
    assert "ANSWERS_JSON" in fake.prompts[0]
    assert json.loads(_apps(conn)[-1]["answers"]) == {"q": "a"}


async def test_send_captcha_removes_the_in_flight_row(conn):
    await _submit(conn, dry_run=True,
                  run_agent=fake_agent(AgentResult("draft_ready", answers={})))
    r = await _submit(conn, dry_run=False,
                      run_agent=fake_agent(AgentResult("captcha")))
    assert not r["ok"] and r["held"]
    assert [a["status"] for a in _apps(conn)] == ["draft"]
    assert "captcha_held" in _event_types(conn)


async def test_send_needs_answer_removes_the_in_flight_row(conn):
    r = await _submit(conn, dry_run=False,
                      run_agent=fake_agent(AgentResult("needs_answer", "PMP?")))
    assert not r["ok"] and r["needs_answer"] == "PMP?"
    assert _apps(conn) == []


@pytest.mark.parametrize("dry_run", [True, False])
async def test_a_needs_answer_with_no_question_is_still_truthy(conn, dry_run):
    """worker.apply_tick parks on `if result.get("needs_answer")`, so a
    bare RESULT:NEEDS_ANSWER: must not report an empty string -- that
    falls through to job_skipped and clears the park."""
    r = await _submit(conn, dry_run=dry_run,
                      run_agent=fake_agent(AgentResult("needs_answer", "")))
    assert r["needs_answer"]
    assert _apps(conn) == []


async def test_permanent_failure_writes_failed_permanent(conn):
    fake = fake_agent(AgentResult("failed", "sso_required"))
    r = await _submit(conn, dry_run=False, run_agent=fake)
    assert not r["ok"]
    row = _apps(conn)[-1]
    assert row["status"] == "failed_permanent"
    assert row["failure_reason"] == "sso_required"


async def test_retryable_failure_promotes_at_max_attempts(conn):
    for _ in range(ats_apply.MAX_ATTEMPTS):
        r = await _submit(conn, dry_run=False,
                          run_agent=fake_agent(AgentResult("failed", "stuck")))
        assert not r["ok"]
    statuses = [a["status"] for a in _apps(conn)]
    assert statuses == ["failed", "failed", "failed_permanent"]
    # and the permanent row now blocks any further attempt
    r = await _submit(conn, dry_run=False,
                      run_agent=fake_agent(AgentResult("applied")))
    assert not r["ok"] and "already has" in r["reason"]


@pytest.mark.parametrize("reason", UNKNOWN_STATE)
async def test_an_unknown_state_send_holds_instead_of_retrying(conn, reason):
    """The agent drove a real browser and then stopped reporting -- it may
    have clicked Submit before it died. Retrying would be a double-submit
    the moment SUBMISSION_IMPLEMENTED flips, so the row must BLOCK."""
    if reason == "agent_error":
        async def runner(prompt, job_id, nonce):
            raise RuntimeError("claude CLI not on PATH")
    else:
        runner = fake_agent(AgentResult("failed", reason))

    r = await _submit(conn, dry_run=False, run_agent=runner)
    assert not r["ok"]
    row = _apps(conn)[-1]
    assert row["status"] == "held_unknown"
    assert row["failure_reason"] == reason
    # and it blocks: the queue can never re-pick this job
    again = await _submit(conn, dry_run=False,
                          run_agent=fake_agent(AgentResult("applied")))
    assert not again["ok"] and "already has" in again["reason"]


@pytest.mark.parametrize("reason", UNKNOWN_STATE)
async def test_the_same_reasons_stay_retryable_on_a_draft(conn, reason):
    """A draft submits nothing, so re-drafting is free and correct."""
    if reason == "agent_error":
        async def runner(prompt, job_id, nonce):
            raise RuntimeError("nope")
    else:
        runner = fake_agent(AgentResult("failed", reason))

    r = await _submit(conn, dry_run=True, run_agent=runner)
    assert not r["ok"]
    row = _apps(conn)[-1]
    assert row["status"] == "failed"
    assert row["failure_reason"] == reason


async def test_an_agent_crash_keeps_the_slug_and_logs_the_detail(conn):
    """failure_reason stays a queryable taxonomy slug (spec 5.3); the
    exception text lands in the event payload instead."""
    async def boom(prompt, job_id, nonce):
        raise RuntimeError("claude CLI not on PATH")

    r = await _submit(conn, dry_run=False, run_agent=boom)
    assert _apps(conn)[-1]["failure_reason"] == "agent_error"
    payload = conn.execute("SELECT payload FROM event WHERE type ="
                           " 'held_unknown'").fetchone()["payload"]
    assert "claude CLI not on PATH" in payload
    assert "claude CLI not on PATH" in r["reason"]


# -- guards ---------------------------------------------------------------

async def test_send_refused_by_kill_switch(conn):
    r = await _submit(conn, dry_run=False)
    assert not r["ok"] and r["unsupported"]
    assert _apps(conn) == []


@pytest.mark.parametrize("source", ["ats", "linkedin", "naukri"])
async def test_kill_switch_gates_every_source(conn, source):
    conn.execute("UPDATE job SET source = ? WHERE id = 1", (source,))
    conn.commit()
    r = await _submit(conn, dry_run=False)
    assert not r["ok"] and r["unsupported"]


def test_submission_stays_disabled():
    assert ats_apply.SUBMISSION_IMPLEMENTED is False


async def test_a_draft_needs_no_kill_switch(conn):
    """Drafting fills a form without submitting; only the send is gated."""
    r = await _submit(conn, dry_run=True,
                      run_agent=fake_agent(AgentResult("draft_ready", answers={})))
    assert r["ok"]


async def test_blocking_status_refuses_new_attempt(conn):
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'base-v1', 'submitted')")
    conn.commit()
    r = await _submit(conn, dry_run=True,
                      run_agent=fake_agent(AgentResult("applied")))
    assert not r["ok"] and "already has" in r["reason"]


async def test_missing_profile_raises(conn):
    with pytest.raises(RuntimeError, match="candidate_profile"):
        await _submit(conn, dry_run=True, profile=None,
                      run_agent=fake_agent(AgentResult("applied")))


async def test_unknown_job_is_reported(conn):
    r = await ats_apply.submit(conn, 99, dry_run=True, brief=BRIEF,
                               profile=PROFILE,
                               run_agent=fake_agent(AgentResult("applied")))
    assert not r["ok"] and "not found" in r["reason"]


async def test_submit_refuses_when_no_resume_is_on_record(conn):
    r = await _submit(conn, dry_run=True, resume_version="tailored-1-r1",
                      run_agent=fake_agent(AgentResult("draft_ready", answers={})))
    assert not r["ok"]
    assert "résumé" in r["reason"] or "resume" in r["reason"].lower()


async def test_draft_stores_the_given_resume_version(conn, tmp_path):
    (tmp_path / "r1.docx").write_text("x", encoding="utf-8")
    conn.execute("INSERT INTO resume (version, path)"
                 " VALUES ('tailored-1-r1', ?)", (str(tmp_path / "r1.docx"),))
    conn.commit()
    r = await _submit(conn, dry_run=True, resume_version="tailored-1-r1",
                      run_agent=fake_agent(AgentResult("draft_ready", answers={})))
    assert r["ok"]
    assert _apps(conn)[0]["resume_version"] == "tailored-1-r1"


async def test_draft_falls_back_to_the_default_resume_version(conn):
    r = await _submit(conn, dry_run=True,
                      run_agent=fake_agent(AgentResult("draft_ready", answers={})))
    assert r["ok"]
    assert _apps(conn)[0]["resume_version"] == ats_apply.RESUME_VERSION


# -- resume text ----------------------------------------------------------

async def test_prompt_resume_text_comes_from_the_docx_body(conn, tmp_path):
    """resume.content holds only the tailored summary/bullets JSON, not the
    work history an employment-history form needs -- the body must come off
    the rendered DOCX."""
    path = tmp_path / "r1.docx"
    doc = docx.Document()
    doc.add_paragraph("Jane Doe -- AI Engineer")
    doc.add_paragraph("Acme Corp, 2021-2024: shipped the thing")
    doc.save(str(path))
    conn.execute("INSERT INTO resume (version, path, content)"
                 " VALUES ('tailored-1-r1', ?, ?)",
                 (str(path), json.dumps({"summary": "Tailored summary here",
                                         "bullets": [{"text": "Bullet one",
                                                      "fact_ids": [1]}]})))
    conn.commit()
    fake = fake_agent(AgentResult("draft_ready", answers={}))
    await _submit(conn, dry_run=True, resume_version="tailored-1-r1",
                  run_agent=fake)
    prompt = fake.prompts[0]
    assert "Acme Corp, 2021-2024: shipped the thing" in prompt
    assert "Tailored summary here" in prompt
    assert "Bullet one" in prompt
    # ... and the upload path is the staged clean copy, not the stored file
    assert _upload_path(prompt).name == "Jane_Doe_Resume.docx"


async def test_an_unreadable_resume_file_does_not_break_the_draft(conn):
    """'r.docx' from the fixture exists but is not a parseable .docx --
    _resume_text degrades rather than raising. (A résumé that is MISSING is
    a different case: submit() refuses, see below.)"""
    fake = fake_agent(AgentResult("draft_ready", answers={}))
    r = await _submit(conn, dry_run=True, run_agent=fake)
    assert r["ok"]
    assert "== RESUME TEXT ==" in fake.prompts[0]


# -- state machine (kept) -------------------------------------------------

def test_stale_in_flight_becomes_held_unknown(conn):
    conn.execute("INSERT INTO application (job_id, resume_version, status,"
                 " started_at) VALUES (1, 'v1', 'in_flight',"
                 " datetime('now', '-30 minutes'))")
    conn.commit()
    assert ats_apply.sweep_stale_in_flight(conn, minutes=15) == 1
    assert _apps(conn)[0]["status"] == "held_unknown"


async def test_a_draft_does_not_block_a_real_submission(conn):
    await _submit(conn, dry_run=True,
                  run_agent=fake_agent(AgentResult("draft_ready", answers={})))
    r = await _submit(conn, dry_run=False,
                      run_agent=fake_agent(AgentResult("applied")))
    assert r["ok"]
    assert {a["status"] for a in _apps(conn)} == {"draft", "submitted"}


async def test_second_real_submission_is_refused(conn):
    await _submit(conn, dry_run=False,
                  run_agent=fake_agent(AgentResult("applied")))
    r = await _submit(conn, dry_run=False,
                      run_agent=fake_agent(AgentResult("applied")))
    assert not r["ok"] and "already" in r["reason"]
    assert len(_apps(conn)) == 1


# -- preconditions: a missing binary must not brick the queue --------------

@pytest.fixture
def no_live_runner(monkeypatch):
    """Belt and braces for the tests below, which are the only ones that
    reach submit() with run_agent=None on a path that could otherwise
    launch Chrome."""
    async def _never(prompt, job_id, nonce):
        raise AssertionError("the live runner must never run in a test")
    monkeypatch.setattr(ats_apply, "_live_run_agent", _never)


@pytest.mark.parametrize("dry_run", [True, False])
async def test_a_missing_binary_writes_no_application_row(conn, monkeypatch,
                                                          no_live_runner, dry_run):
    """`npx` off PATH means no browser launched and nothing submitted. It
    must not become held_unknown -- in auto mode that converts the whole
    queue to a permanently stuck state over a missing binary."""
    def boom():
        raise agent_mod.PreconditionError("agentic apply needs `npx` on PATH")
    monkeypatch.setattr(ats_apply, "preflight", boom)
    # the kill switch would otherwise mask the send path before preflight
    monkeypatch.setattr(ats_apply, "SUBMISSION_IMPLEMENTED", True)

    r = await _submit(conn, dry_run=dry_run)
    assert not r["ok"] and "npx" in r["reason"]
    assert conn.execute("SELECT COUNT(*) n FROM application").fetchone()["n"] == 0
    assert _event_types(conn) == []
    # ... and the refusal must carry the flag worker.apply_tick pauses on:
    # no row means QUEUE_WHERE re-admits this job, so clearing the park
    # instead would re-pick it on the very next tick, forever.
    assert r["unsupported"]


async def test_preflight_is_skipped_when_a_runner_is_injected(conn, monkeypatch):
    """Preconditions describe what _live_run_agent needs; no test on this
    machine may require the claude/npx/Chrome binaries."""
    def boom():
        raise AssertionError("preflight must not run for an injected runner")
    monkeypatch.setattr(ats_apply, "preflight", boom)
    r = await _submit(conn, dry_run=True,
                      run_agent=fake_agent(AgentResult("draft_ready", answers={})))
    assert r["ok"]


async def test_a_precondition_escaping_mid_run_is_not_unknown_state(conn):
    """The backstop check inside _run_agent_blocking: nothing launched, so
    the send path must not hold it as 'possibly submitted'."""
    async def boom(prompt, job_id, nonce):
        raise agent_mod.PreconditionError("Chrome not found -- set CHROME_PATH")

    r = await _submit(conn, dry_run=False, run_agent=boom)
    assert not r["ok"]
    row = _apps(conn)[-1]
    assert row["status"] == "failed"            # retryable, not held_unknown
    assert row["failure_reason"] == "precondition"


# -- the sweep/late-return double-submit race ------------------------------

def _sweeping_agent(conn, result):
    """A run that outlives sweep_stale_in_flight's 15-minute window:
    timeout_s is not a wall-clock bound, so the sweep flips the row to
    held_unknown while the agent is still driving the browser."""
    async def _fake(prompt, job_id, nonce):
        conn.execute("UPDATE application SET started_at ="
                     " datetime('now', '-30 minutes') WHERE status = 'in_flight'")
        conn.commit()
        assert ats_apply.sweep_stale_in_flight(conn) == 1
        return result
    return _fake


async def test_a_late_failure_does_not_reopen_a_swept_row(conn):
    """Turning held_unknown back into 'failed' re-admits the job to the
    queue and applies a second time to a form the first run may already
    have submitted."""
    r = await _submit(conn, dry_run=False,
                      run_agent=_sweeping_agent(conn, AgentResult("failed", "stuck")))
    assert not r["ok"]
    row = _apps(conn)[-1]
    assert row["status"] == "held_unknown"
    assert row["status"] in ats_apply.BLOCKING     # ... so QUEUE_WHERE skips it
    again = await _submit(conn, dry_run=False,
                          run_agent=fake_agent(AgentResult("applied")))
    assert not again["ok"] and "already has" in again["reason"]


async def test_a_late_applied_still_upgrades_a_swept_row(conn):
    """The truthful upgrade must never be suppressed: the form really was
    submitted."""
    r = await _submit(conn, dry_run=False,
                      run_agent=_sweeping_agent(conn, AgentResult("applied")))
    assert r["ok"] and r["status"] == "submitted"
    assert _apps(conn)[-1]["status"] == "submitted"


# -- the résumé the agent is told to upload --------------------------------

async def test_the_agent_gets_an_absolute_resume_path(conn, tmp_path, monkeypatch):
    """tailor.py stores resume.path relative to backend/, but the agent's
    cwd is a temp dir outside the repo -- a relative path there resolves to
    nothing, and uploading the tailored résumé is the whole point of the
    run. Same bug class as the relative --mcp-config path."""
    rel = Path("resume/generated/tailored-1-r1.docx")
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)                       # ... so `rel` is live
    conn.execute("INSERT INTO resume (version, path) VALUES ('tailored-1-r1', ?)",
                 (str(rel),))
    conn.commit()

    fake = fake_agent(AgentResult("draft_ready", answers={}))
    r = await _submit(conn, dry_run=True, resume_version="tailored-1-r1",
                      run_agent=fake)
    assert r["ok"]
    given = _upload_path(fake.prompts[0])
    assert given.is_absolute(), given
    assert given.exists()
    assert given.parent == (tmp_path / "work" / "job1").resolve()


async def test_a_missing_resume_file_refuses_and_writes_nothing(conn):
    """Better to refuse than to start a browser session that can only fail
    at the upload step -- same spirit as the binary preflight."""
    conn.execute("INSERT INTO resume (version, path)"
                 " VALUES ('tailored-1-r1', 'no/such/resume.docx')")
    conn.commit()
    r = await _submit(conn, dry_run=True, resume_version="tailored-1-r1",
                      run_agent=fake_agent(AgentResult("draft_ready", answers={})))
    assert not r["ok"]
    assert "résumé" in r["reason"] or "resume" in r["reason"].lower()
    assert _apps(conn) == []
    assert _event_types(conn) == []
    # pauses the worker rather than being re-picked every 0.1s: no row means
    # QUEUE_WHERE still admits this job (same reason as the preflight above)
    assert r["unsupported"]


# -- F1: the filename a recruiter sees --------------------------------------

def _upload_path(prompt: str) -> Path:
    line = next(l for l in prompt.splitlines() if "upload this exact file" in l)
    return Path(line.rsplit(": ", 1)[1])


async def test_the_resume_is_staged_under_the_candidates_own_name(
        conn, tmp_path, monkeypatch):
    """A recruiter opening the attachment must not read
    'tailored-1-r1.docx' and learn the résumé was machine-generated per
    job (spec 3.2 FILES). The agent is handed a clean copy instead."""
    src = tmp_path / "tailored-1-r1.docx"
    src.write_text("the rendered docx", encoding="utf-8")
    conn.execute("INSERT INTO resume (version, path)"
                 " VALUES ('tailored-1-r1', ?)", (str(src),))
    conn.commit()

    fake = fake_agent(AgentResult("draft_ready", answers={}))
    r = await _submit(conn, dry_run=True, resume_version="tailored-1-r1",
                      run_agent=fake)
    assert r["ok"]
    given = _upload_path(fake.prompts[0])
    assert given.name == "Jane_Doe_Resume.docx"     # PROFILE is "Jane Doe"
    assert given.is_absolute() and given.exists()
    assert given.read_text(encoding="utf-8") == "the rendered docx"
    assert given.parent == (tmp_path / "work" / "job1").resolve()
    assert src.exists()                             # the original is untouched


async def test_a_name_with_path_characters_still_makes_a_legal_filename(
        conn, tmp_path):
    profile = CandidateProfile(candidate_name="A/B  C:D",
                               candidate_email="a@example.com",
                               candidate_phone="+91-90000-00000")
    fake = fake_agent(AgentResult("draft_ready", answers={}))
    r = await _submit(conn, dry_run=True, profile=profile, run_agent=fake)
    assert r["ok"]
    assert _upload_path(fake.prompts[0]).name == "A_B_C_D_Resume.docx"


# -- F3: what a run cost survives the run ----------------------------------

async def test_the_run_cost_and_duration_are_logged(conn, caplog):
    """AgentResult carries cost_usd/duration_ms and nothing read them: after
    a live trial there would be no record of what any run cost. No DB column
    for P0 -- the log line and the transcript footer are the record."""
    fake = fake_agent(AgentResult("draft_ready", answers={},
                                  cost_usd=0.0421, duration_ms=12345))
    with caplog.at_level(logging.INFO, logger="career_agent.apply.ats"):
        await _submit(conn, dry_run=True, run_agent=fake)
    assert "0.0421" in caplog.text
    assert "12345" in caplog.text
    assert "job 1" in caplog.text


# -- F4: a real submission with an empty audit trail -----------------------

async def test_an_auto_submit_with_no_answers_is_recorded_and_flagged(conn):
    """parse_result only demands ANSWERS_JSON for DRAFT_READY, so in auto
    mode (pinned is None) a bare RESULT:APPLIED records a REAL submission
    with answers = '{}' -- silently. The send happened and must still be
    recorded; the gap has to be visible."""
    r = await _submit(conn, dry_run=False,
                      run_agent=fake_agent(AgentResult("applied")))
    assert r["ok"] and r["status"] == "submitted"
    row = _apps(conn)[-1]
    assert row["status"] == "submitted"          # never lose the send itself
    assert row["submitted_at"] is not None
    assert row["failure_reason"] == "answers_json_missing"
    assert "answers_json_missing" in _event_types(conn)


async def test_a_submit_that_reported_answers_is_not_flagged(conn):
    await _submit(conn, dry_run=False,
                  run_agent=fake_agent(AgentResult("applied", answers={"q": "a"})))
    row = _apps(conn)[-1]
    assert row["status"] == "submitted"
    assert row["failure_reason"] is None
    assert "answers_json_missing" not in _event_types(conn)


async def test_a_send_falling_back_to_pinned_answers_is_not_flagged(conn):
    """The pinned answers ARE the audit trail -- nothing is missing."""
    await _submit(conn, dry_run=True,
                  run_agent=fake_agent(AgentResult("draft_ready",
                                                   answers={"Visa?": "Citizen"})))
    await _submit(conn, dry_run=False,
                  run_agent=fake_agent(AgentResult("applied")))
    row = _apps(conn)[-1]
    assert row["failure_reason"] is None
    assert json.loads(row["answers"]) == {"Visa?": "Citizen"}


# -- F6: agent runs are serialized -----------------------------------------

def _second_job(conn):
    conn.execute("INSERT INTO job (fingerprint, source, external_id, company,"
                 " company_normalized, title, title_normalized, url)"
                 " VALUES ('fp2','ats','2','Beta','beta','AI Engineer',"
                 " 'aiengineer','https://y/apply')")
    conn.commit()
    return conn.execute("SELECT id FROM job WHERE fingerprint = 'fp2'"
                        ).fetchone()["id"]


async def test_two_agent_runs_never_overlap(conn):
    """chrome.launch_chrome _kill_port(9222)s before every launch, and
    _run_agent_blocking wipes the shared session dir and rewrites the shared
    .mcp-apply.json -- one port, one profile, one session dir. A dashboard
    click during an auto-worker run would taskkill the in-flight Chrome
    mid-submission."""
    job2 = _second_job(conn)
    live = 0
    peak = 0

    async def runner(prompt, job_id, nonce):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)          # the browser session
        live -= 1
        return AgentResult("draft_ready", answers={})

    await asyncio.gather(
        ats_apply.submit(conn, 1, dry_run=True, brief=BRIEF, profile=PROFILE,
                         run_agent=runner),
        ats_apply.submit(conn, job2, dry_run=True, brief=BRIEF, profile=PROFILE,
                         run_agent=runner))
    assert peak == 1, f"{peak} agent runs were in flight at once"
    assert {a["status"] for a in conn.execute(
        "SELECT status FROM application").fetchall()} == {"draft"}


async def test_a_queued_send_does_not_age_its_own_in_flight_row(conn):
    """The in_flight row is written inside the lock, not before it: a run
    waiting its turn would otherwise have started_at ticking toward
    sweep_stale_in_flight's 15 minutes while it had not begun."""
    job2 = _second_job(conn)
    started = []

    async def runner(prompt, job_id, nonce):
        row = conn.execute("SELECT COUNT(*) n FROM application"
                           " WHERE status = 'in_flight'").fetchone()
        started.append(row["n"])
        await asyncio.sleep(0.02)
        return AgentResult("applied", answers={"q": "a"})

    await asyncio.gather(
        ats_apply.submit(conn, 1, dry_run=False, brief=BRIEF, profile=PROFILE,
                         run_agent=runner),
        ats_apply.submit(conn, job2, dry_run=False, brief=BRIEF, profile=PROFILE,
                         run_agent=runner))
    assert started == [1, 1], f"in_flight rows seen per run: {started}"


# -- F7: clearing a held_unknown (web/actions.queue_retry) -----------------
# Imported inside each test: this file must stay collectable even if the web
# package's import chain breaks on a given machine.

def _held(conn, reason="agent_error"):
    conn.execute("INSERT INTO application (job_id, resume_version, status,"
                 " failure_reason) VALUES (1, 'base-v1', 'held_unknown', ?)",
                 (reason,))
    conn.commit()


def test_a_held_application_is_not_cleared_without_confirmation(conn):
    """Held means 'the agent may already have submitted'. Releasing it on a
    plain retry click would re-admit the job and apply a second time."""
    from career_agent.web import actions

    _held(conn)
    r = actions.queue_retry(conn, 1)
    assert not r["ok"]
    assert "held" in r["message"].lower() or "confirm" in r["message"].lower()
    assert _apps(conn)[-1]["status"] == "held_unknown"


def test_confirming_it_was_not_submitted_clears_the_hold(conn):
    """The applications page says 'Held -- confirm manually, then clear it'
    and no control did: queue_retry took only 'failed', and mark_applied hit
    the one_live_application_per_job index. Raw SQL was the only exit."""
    from career_agent.web import actions

    _held(conn)
    r = actions.queue_retry(conn, 1, confirm_not_submitted=True)
    assert r["ok"]
    row = _apps(conn)[-1]
    assert row["status"] == "failed"               # retryable, not BLOCKING
    assert row["status"] not in ats_apply.BLOCKING
    assert row["failure_reason"] == "agent_error"  # why it was held is kept
    assert "hold_cleared" in _event_types(conn)


async def test_clearing_a_hold_lets_the_job_be_attempted_again(conn):
    from career_agent.web import actions

    _held(conn)
    actions.queue_retry(conn, 1, confirm_not_submitted=True)
    r = await _submit(conn, dry_run=True,
                      run_agent=fake_agent(AgentResult("draft_ready", answers={})))
    assert r["ok"], r


def test_confirmation_does_nothing_for_any_other_status(conn):
    """The flag is an escape hatch for held_unknown only -- it must not turn
    a 'submitted' row back into a retry."""
    from career_agent.web import actions

    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'base-v1', 'submitted')")
    conn.commit()
    r = actions.queue_retry(conn, 1, confirm_not_submitted=True)
    assert not r["ok"]
    assert _apps(conn)[-1]["status"] == "submitted"


def test_a_failed_application_still_retries_without_the_flag(conn):
    from career_agent.web import actions

    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'base-v1', 'failed')")
    conn.commit()
    assert actions.queue_retry(conn, 1)["ok"]


def test_the_human_can_say_it_WAS_submitted(conn):
    """The other exit the page promises: confirm on the employer's site that
    it went through. mark_applied promotes the held row rather than hitting
    the one_live_application_per_job index."""
    from career_agent import store as store_mod

    _held(conn)
    conn.execute("UPDATE application SET answers = '{\"q\": \"a\"}'")
    conn.commit()
    app_id = store_mod.mark_applied(conn, 1, "2026-09-01")
    rows = _apps(conn)
    assert len(rows) == 1 and rows[0]["id"] == app_id
    assert rows[0]["status"] == "submitted"
    assert rows[0]["submitted_at"] == "2026-09-01"
    assert json.loads(rows[0]["answers"]) == {"q": "a"}, (
        "unlike a draft, a held row's answers are what the agent actually"
        " typed into the live form -- the only audit trail there is")


# -- F8: submit() is what keeps the two sides of the contract together -----

async def test_the_runner_is_handed_the_prompts_own_nonce(conn):
    """build_prompt stamps it and parse_result demands it -- submit() is the
    only place both are chosen, so they cannot drift apart at runtime."""
    fake = fake_agent(AgentResult("draft_ready", answers={}))
    await _submit(conn, dry_run=True, run_agent=fake)
    nonce = fake.nonces[0]
    assert nonce
    assert f"RESULT:{nonce}:APPLIED" in fake.prompts[0]
    assert "RESULT:" not in fake.prompts[0].replace(f"RESULT:{nonce}:", "")


async def test_every_run_gets_a_fresh_nonce(conn):
    fake = fake_agent(AgentResult("failed", "stuck"))
    await _submit(conn, dry_run=True, run_agent=fake)
    await _submit(conn, dry_run=True, run_agent=fake)
    assert fake.nonces[0] != fake.nonces[1]


# -- the sweep race on the captcha / needs_answer delete paths -------------

@pytest.mark.parametrize("result,key", [
    (AgentResult("captcha"), "held"),
    (AgentResult("needs_answer", "PMP?"), "needs_answer"),
])
async def test_a_late_hold_does_not_delete_a_swept_row(conn, result, key):
    """Same race the failure path already closed: a run can outlive
    sweep_stale_in_flight and come back to find its own row held_unknown.
    Deleting it would re-admit a job the first run may already have
    submitted."""
    r = await _submit(conn, dry_run=False,
                      run_agent=_sweeping_agent(conn, result))
    assert not r["ok"] and r[key]
    row = _apps(conn)[-1]
    assert row["status"] == "held_unknown"
    again = await _submit(conn, dry_run=False,
                          run_agent=fake_agent(AgentResult("applied")))
    assert not again["ok"] and "already has" in again["reason"]


async def test_two_jobs_do_not_share_one_staged_resume(conn, tmp_path):
    """Staging happens before _agent_lock() is taken, and the basename is
    the same for every job -- one shared path would let a second submit()
    overwrite the copy the first run is about to upload, and send the wrong
    résumé to a real employer."""
    job2 = _second_job(conn)
    for version, body, job in (("tailored-1-r1", "resume for job 1", 1),
                               ("tailored-2-r1", "resume for job 2", job2)):
        src = tmp_path / f"{version}.docx"
        src.write_text(body, encoding="utf-8")
        conn.execute("INSERT INTO resume (version, path) VALUES (?, ?)",
                     (version, str(src)))
    conn.commit()

    staged = {}

    async def runner(prompt, job_id, nonce):
        staged[job_id] = _upload_path(prompt)
        await asyncio.sleep(0.01)
        return AgentResult("draft_ready", answers={})

    await asyncio.gather(
        ats_apply.submit(conn, 1, dry_run=True, brief=BRIEF, profile=PROFILE,
                         resume_version="tailored-1-r1", run_agent=runner),
        ats_apply.submit(conn, job2, dry_run=True, brief=BRIEF, profile=PROFILE,
                         resume_version="tailored-2-r1", run_agent=runner))

    assert staged[1] != staged[job2]
    assert staged[1].name == staged[job2].name == "Jane_Doe_Resume.docx"
    assert staged[1].read_text(encoding="utf-8") == "resume for job 1"
    assert staged[job2].read_text(encoding="utf-8") == "resume for job 2"
