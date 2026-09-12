import json

import docx
import pytest

from career_agent import db, store
from career_agent.apply import agent as agent_mod
from career_agent.apply import ats as ats_apply
from career_agent.apply.agent import AgentResult
from career_agent.config import CandidateProfile, CareerBrief


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    c.execute("INSERT INTO job (fingerprint, source, external_id, company,"
              " company_normalized, title, title_normalized, url)"
              " VALUES ('fp','ats','1','Acme','acme','AI Engineer',"
              " 'aiengineer','https://x/apply')")
    c.execute("INSERT INTO resume (version, path) VALUES ('base-v1', 'r.docx')")
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
    async def _fake(prompt, job_id):
        _fake.prompts.append(prompt)
        _fake.job_ids.append(job_id)
        return result
    _fake.prompts = []
    _fake.job_ids = []
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
        async def runner(prompt, job_id):
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
        async def runner(prompt, job_id):
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
    async def boom(prompt, job_id):
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


async def test_draft_stores_the_given_resume_version(conn):
    conn.execute("INSERT INTO resume (version, path)"
                 " VALUES ('tailored-1-r1', 'r1.docx')")
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
    assert str(path) in prompt          # the upload path itself


async def test_an_unreadable_resume_file_does_not_break_the_draft(conn):
    """'r.docx' from the fixture does not exist on disk."""
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
    async def _never(prompt, job_id):
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
    async def boom(prompt, job_id):
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
    async def _fake(prompt, job_id):
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
