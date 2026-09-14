import asyncio
import json
import sqlite3

import pytest

from career_agent import db, store, tailor
from career_agent.config import CandidateProfile
from career_agent.web import worker
from conftest import build_tailor_template


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    for i in range(10):
        c.execute("INSERT INTO fact (claim, evidence) VALUES (?, ?)",
                  (f"claim {i}", f"evidence {i}"))
    c.commit()
    return c


@pytest.fixture(autouse=True)
def stub_tailoring(monkeypatch, tmp_path):
    """apply_tick now tailors before drafting. Default every test to a safe,
    deterministic tailoring path -- no real LLM call, no
    CLAUDE_CODE_OAUTH_TOKEN dependency -- so tests that only care about the
    apply-queue state machine keep working unchanged."""
    template = tmp_path / "master.docx"
    build_tailor_template(template)
    monkeypatch.setattr(tailor, "TEMPLATE_PATH", template)
    monkeypatch.setattr(tailor, "OUTPUT_DIR", tmp_path / "generated")
    monkeypatch.setattr(worker.run_module, "verify_auth", lambda: None)

    async def _default_ask(prompt, model=None):
        return json.dumps({"summary": "Tailored summary.",
                           "bullets": [{"text": "Relevant bullet",
                                       "fact_ids": [1]}]})
    monkeypatch.setattr(worker.run_module, "_ask", _default_ask)


@pytest.fixture
def brief_path(tmp_path):
    p = tmp_path / "career_brief.toml"
    p.write_text(
        'target_titles = ["AI Engineer"]\n'
        'search_locations = ["Chennai"]\n'
        'daily_cap = 5\n')
    return p


@pytest.fixture
def profile_path(tmp_path):
    p = tmp_path / "candidate_profile.toml"
    p.write_text('candidate_name = "Jane Doe"\n'
                 'candidate_email = "jane@example.com"\n'
                 'candidate_phone = "+91-90000-00000"\n')
    return p


def _job(conn, fp, priority=None, score=80, verdict="submit"):
    job_id = conn.execute(
        "INSERT INTO job (fingerprint, source, external_id, company,"
        " company_normalized, title, title_normalized, priority)"
        " VALUES (?, 'ats', ?, 'Acme', 'acme', 'AI Engineer',"
        " 'aiengineer', ?)", (fp, fp, priority)).lastrowid
    conn.execute(
        "INSERT INTO assessment (job_id, stage, weighted_score, verdict,"
        " rationale, model, prompt_version)"
        " VALUES (?, 'scored', ?, ?, 'r', 'm', 'v1')",
        (job_id, score, verdict))
    conn.commit()
    return job_id


async def test_apply_tick_passes_brief_and_profile_to_submit(
        conn, brief_path, profile_path, monkeypatch):
    _job(conn, "fp1")
    conn.execute("INSERT INTO resume (version, path) VALUES ('base-v1', 'r.docx')")
    conn.commit()
    worker.set_run_state(conn, "apply", status="running", mode="manual")

    captured = {}

    async def fake_submit(conn, job_id, mode, brief=None, profile=None,
                          resume_version=None, **kw):
        captured["brief"] = brief
        captured["profile"] = profile
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)

    await worker.apply_tick(conn, brief_path, profile_path)

    assert captured["brief"] is not None
    assert captured["profile"].candidate_name == "Jane Doe"


async def test_apply_tick_errors_when_candidate_profile_is_missing(
        conn, brief_path, tmp_path, monkeypatch):
    """The agent needs candidate_profile.toml for every source now (Task 6):
    submit() raises RuntimeError on profile=None, and apply_tick has no
    fallback left -- a missing profile is a run_state error, not a silent
    None passed through."""
    _job(conn, "fp1")
    conn.execute("INSERT INTO resume (version, path) VALUES ('base-v1', 'r.docx')")
    conn.commit()
    worker.set_run_state(conn, "apply", status="running", mode="manual")

    async def fail_if_called(*a, **kw):
        raise AssertionError("submit should not be reached without a profile")

    monkeypatch.setattr(worker.ats_apply, "submit", fail_if_called)

    missing_profile_path = tmp_path / "no_such_candidate_profile.toml"
    await worker.apply_tick(conn, brief_path, missing_profile_path)

    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "error"
    assert "candidate_profile.toml" in state["last_error"]
    assert state["current_job_id"] is None


async def test_apply_tick_parks_on_needs_answer_instead_of_looping(
        conn, brief_path, profile_path, monkeypatch):
    job_id = _job(conn, "fp1")
    worker.set_run_state(conn, "apply", status="running", mode="manual")

    async def fake_submit(conn, job_id, mode, brief=None, profile=None,
                          resume_version=None, **kw):
        return {"ok": False, "needs_answer": "Notice period?"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)

    await worker.apply_tick(conn, brief_path, profile_path)

    state = worker.get_run_state(conn, "apply")
    assert state["current_job_id"] == job_id, "must stay parked, not cleared"
    events = [e["type"] for e in conn.execute("SELECT type FROM event")]
    assert "needs_answer" in events

    # A second tick while parked must not re-attempt: current_job_id is
    # still set, so apply_tick's own early-return guard applies.
    calls = []
    async def counting_submit(*a, **kw):
        calls.append(1)
        return {"ok": False, "needs_answer": "Notice period?"}
    monkeypatch.setattr(worker.ats_apply, "submit", counting_submit)
    await worker.apply_tick(conn, brief_path, profile_path)
    assert calls == [], "a parked run must not re-pick or re-submit"


async def test_apply_tick_tailors_before_drafting_and_threads_the_version(
        conn, brief_path, profile_path, monkeypatch):
    _job(conn, "fp1")
    worker.set_run_state(conn, "apply", status="running", mode="manual")

    captured = {}

    async def fake_submit(conn, job_id, mode, resume_version=None, **kw):
        captured["resume_version"] = resume_version
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)

    await worker.apply_tick(conn, brief_path, profile_path)

    assert captured["resume_version"] == "tailored-1-r1"
    row = conn.execute("SELECT * FROM resume WHERE version = ?",
                       (captured["resume_version"],)).fetchone()
    assert row is not None


async def test_apply_tick_reuses_an_already_tailored_resume(conn, brief_path,
                                                             profile_path,
                                                             monkeypatch):
    job_id = _job(conn, "fp1")
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES ('tailored-1-r1', 'x.docx', ?)", (job_id,))
    conn.commit()
    worker.set_run_state(conn, "apply", status="running", mode="manual")

    async def must_not_tailor(prompt, model=None):
        raise AssertionError("must not tailor again")

    monkeypatch.setattr(worker.run_module, "_ask", must_not_tailor)

    captured = {}

    async def fake_submit(conn, job_id, mode, resume_version=None, **kw):
        captured["resume_version"] = resume_version
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)

    await worker.apply_tick(conn, brief_path, profile_path)

    assert captured["resume_version"] == "tailored-1-r1"


def test_get_run_state_returns_seeded_apply_row(conn):
    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "idle"
    assert state["mode"] == "manual"


def test_set_run_state_updates_given_fields_only(conn):
    worker.set_run_state(conn, "apply", status="running", mode="auto")
    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "running"
    assert state["mode"] == "auto"
    # pipeline row untouched
    assert worker.get_run_state(conn, "pipeline")["status"] == "idle"


def test_set_run_state_with_no_fields_is_a_noop(conn):
    worker.set_run_state(conn, "apply")  # would be "SET , updated_at = ..."
    assert worker.get_run_state(conn, "apply")["status"] == "idle"


def test_next_candidate_orders_by_score_when_no_priority(conn):
    _job(conn, "low", score=50)
    high = _job(conn, "high", score=90)
    assert worker.next_candidate(conn)["job_id"] == high


def test_next_candidate_priority_overrides_score(conn):
    _job(conn, "high-score", score=99, priority=None)
    low_score_high_priority = _job(conn, "prioritized", score=1, priority=0)
    assert worker.next_candidate(conn)["job_id"] == low_score_high_priority


def test_next_candidate_excludes_skip_verdict(conn):
    _job(conn, "skipped", verdict="skip")
    assert worker.next_candidate(conn) is None


def test_next_candidate_excludes_jobs_with_a_live_application(conn):
    job_id = _job(conn, "applied")
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (?, 'v1', 'submitted')", (job_id,))
    conn.commit()
    assert worker.next_candidate(conn) is None


def test_next_candidate_excludes_jobs_with_a_pending_draft(conn):
    """A drafted job is already in flight from the queue's point of view --
    otherwise the background loop re-picks it a second after it was skipped."""
    job_id = _job(conn, "drafted")
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (?, 'v1', 'draft')", (job_id,))
    conn.commit()
    assert worker.next_candidate(conn) is None
    assert worker.queue_count(conn) == 0


def test_next_candidate_includes_a_job_that_drafted_then_failed(conn):
    """A real-submit attempt never deletes the earlier draft row (ats.py's
    submit() only inserts), so a job that drafted and then failed transiently
    carries BOTH a 'draft' row and a later 'failed' row. The latest row is
    what matters -- it must stay retryable, or auto-mode retry and
    /queue/{id}/retry become permanent no-ops after the first attempt."""
    job_id = _job(conn, "drafted-then-failed")
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (?, 'v1', 'draft')", (job_id,))
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (?, 'v1', 'failed')", (job_id,))
    conn.commit()
    assert worker.next_candidate(conn)["job_id"] == job_id
    assert worker.queue_count(conn) == 1


def test_next_candidate_includes_a_failed_job(conn):
    job_id = _job(conn, "failed-once")
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (?, 'v1', 'failed')", (job_id,))
    conn.commit()
    assert worker.next_candidate(conn)["job_id"] == job_id


def test_queue_count_matches_number_of_selectable_candidates(conn):
    _job(conn, "one")
    _job(conn, "two")
    _job(conn, "skip-me", verdict="skip")
    assert worker.queue_count(conn) == 2


def test_next_candidate_excludes_merged_jobs(conn):
    survivor = _job(conn, "survivor")
    dupe = _job(conn, "dupe")
    conn.execute("UPDATE job SET merged_into_job_id = ? WHERE id = ?",
                 (survivor, dupe))
    conn.commit()
    ids = set()
    row = worker.next_candidate(conn)
    while row:
        ids.add(row["job_id"])
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'submitted')", (row["job_id"],))
        conn.commit()
        row = worker.next_candidate(conn)
    assert dupe not in ids


def test_next_candidate_respects_latest_assessment_verdict(conn):
    """Jobs rescored to 'skip' should be excluded even if old assessment was 'submit'."""
    job_id = conn.execute(
        "INSERT INTO job (fingerprint, source, external_id, company,"
        " company_normalized, title, title_normalized)"
        " VALUES (?, 'ats', ?, 'Acme', 'acme', 'AI Engineer',"
        " 'aiengineer')", ("rescored", "rescored")).lastrowid
    # Old assessment: submit
    conn.execute(
        "INSERT INTO assessment (job_id, stage, weighted_score, verdict,"
        " rationale, model, prompt_version)"
        " VALUES (?, 'scored', ?, ?, 'r', 'm', 'v1')",
        (job_id, 85, "submit"))
    # New assessment: skip (newer, should be used)
    conn.execute(
        "INSERT INTO assessment (job_id, stage, weighted_score, verdict,"
        " rationale, model, prompt_version)"
        " VALUES (?, 'scored', ?, ?, 'r', 'm', 'v2')",
        (job_id, 30, "skip"))
    conn.commit()
    # Job should NOT appear because latest assessment is 'skip'
    assert worker.next_candidate(conn) is None
    assert worker.queue_count(conn) == 0


def test_guard_blocks_a_skip_without_override(conn, tmp_path):
    job_id = _job(conn, "skip-me", verdict="skip")
    brief_path = tmp_path / "career_brief.toml"
    brief_path.write_text(
        'target_titles = ["AI Engineer"]\n'
        'search_locations = ["Chennai"]\n'
        'daily_cap = 5\n')
    denial = worker.guard(conn, job_id, allow_skip=False, brief_path=brief_path)
    assert denial is not None
    assert "override" in denial.lower()


def test_guard_allows_a_skip_with_override(conn, tmp_path):
    job_id = _job(conn, "skip-me-2", verdict="skip")
    brief_path = tmp_path / "career_brief.toml"
    brief_path.write_text(
        'target_titles = ["AI Engineer"]\n'
        'search_locations = ["Chennai"]\n'
        'daily_cap = 5\n')
    assert worker.guard(conn, job_id, allow_skip=True,
                         brief_path=brief_path) is None


def test_queue_count_respects_latest_assessment_verdict(conn):
    """queue_count should also only consider latest assessment per job."""
    job_id = conn.execute(
        "INSERT INTO job (fingerprint, source, external_id, company,"
        " company_normalized, title, title_normalized)"
        " VALUES (?, 'ats', ?, 'Acme', 'acme', 'AI Engineer',"
        " 'aiengineer')", ("rescored2", "rescored2")).lastrowid
    # Old assessment: submit
    conn.execute(
        "INSERT INTO assessment (job_id, stage, weighted_score, verdict,"
        " rationale, model, prompt_version)"
        " VALUES (?, 'scored', ?, ?, 'r', 'm', 'v1')",
        (job_id, 85, "submit"))
    # New assessment: skip (newer, should be used)
    conn.execute(
        "INSERT INTO assessment (job_id, stage, weighted_score, verdict,"
        " rationale, model, prompt_version)"
        " VALUES (?, 'scored', ?, ?, 'r', 'm', 'v2')",
        (job_id, 30, "skip"))
    conn.commit()
    # Count should be 0 because latest assessment is 'skip'
    assert worker.queue_count(conn) == 0


async def test_tick_does_nothing_when_not_running(conn, brief_path, profile_path):
    job_id = _job(conn, "idle-test")
    await worker.apply_tick(conn, brief_path, profile_path)
    assert worker.get_run_state(conn, "apply")["current_job_id"] is None
    assert conn.execute("SELECT COUNT(*) n FROM application").fetchone()["n"] == 0


async def test_tick_with_empty_queue_goes_idle_and_logs_completion(
        conn, brief_path, profile_path):
    worker.set_run_state(conn, "apply", status="running", mode="auto")
    await worker.apply_tick(conn, brief_path, profile_path)
    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "idle"
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "run_completed" in types


async def test_auto_mode_makes_exactly_one_submit_call(conn, brief_path,
                                                        profile_path,
                                                        monkeypatch):
    """Auto mode used to draft then immediately send -- two browser sessions
    seconds apart with no human in between. The agent does both jobs in a
    single session now, so auto mode makes exactly one call, in auto mode."""
    job_id = _job(conn, "auto-me")
    worker.set_run_state(conn, "apply", status="running", mode="auto")

    calls = []

    async def fake_submit(conn, job_id, mode, resume_version=None, **kw):
        calls.append(mode)
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'submitted')", (job_id,))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "submitted"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)
    await worker.apply_tick(conn, brief_path, profile_path)

    assert calls == ["auto"]
    assert worker.get_run_state(conn, "apply")["current_job_id"] is None


async def test_tick_auto_mode_skips_when_the_real_send_reports_not_ok(
        conn, brief_path, profile_path, monkeypatch):
    """A captcha hold or permanent failure comes back as ok=False, not as an
    exception -- treating that as success would leave the run claiming it
    applied and, worse, silently move on with no job_skipped record."""
    job_id = _job(conn, "captcha")
    worker.set_run_state(conn, "apply", status="running", mode="auto")

    async def fake_submit(conn, job_id, mode, resume_version=None, **kw):
        return {"ok": False, "reason": "captcha held the submission"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)
    await worker.apply_tick(conn, brief_path, profile_path)

    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "running"
    assert state["current_job_id"] is None
    skips = conn.execute(
        "SELECT payload FROM event WHERE type = 'job_skipped'").fetchall()
    assert [s["payload"] for s in skips] == ["captcha held the submission"]


async def test_auto_mode_pauses_when_submit_reports_unsupported(
        conn, brief_path, profile_path, monkeypatch):
    """submit() refuses a real send outright (unsupported=True) while
    SUBMISSION_IMPLEMENTED stays False, and writes NO application row when
    it does -- so QUEUE_WHERE never excludes this job. Clearing
    current_job_id the way an ordinary not-ok result does would let
    apply_worker_loop re-pick this exact job on the very next tick and spin
    forever. Pausing the run (like the daily-cap path) says so once."""
    job_id = _job(conn, "unsupported")
    worker.set_run_state(conn, "apply", status="running", mode="auto")

    calls = []

    async def fake_submit(conn, job_id, mode, resume_version=None, **kw):
        calls.append(1)
        return {"ok": False, "unsupported": True,
                "reason": "real sends are not enabled yet"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)
    await worker.apply_tick(conn, brief_path, profile_path)

    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "paused"
    assert state["current_job_id"] is None
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "run_autopaused" in types
    assert worker.next_candidate(conn)["job_id"] == job_id, (
        "no application row was written, so the job is still queueable --"
        " it's the paused run_state that must stop the loop, not the queue")

    # A second tick while paused must not re-pick the job: apply_tick's own
    # early-return guard (status != 'running') applies.
    await worker.apply_tick(conn, brief_path, profile_path)
    assert calls == [1], "a paused run must not re-pick or re-submit"


async def test_needs_answer_from_auto_send_parks(conn, brief_path,
                                                  profile_path, monkeypatch):
    job_id = _job(conn, "needs-answer-auto")
    worker.set_run_state(conn, "apply", status="running", mode="auto")

    async def fake_submit(conn, job_id, mode, resume_version=None, **kw):
        return {"ok": False, "needs_answer": "PMP cert?",
                "reason": "needs an answer: PMP cert?"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)
    await worker.apply_tick(conn, brief_path, profile_path)

    state = worker.get_run_state(conn, "apply")
    assert state["current_job_id"] == job_id
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "needs_answer" in types


async def test_tick_is_a_noop_while_awaiting_manual_review(
        conn, brief_path, profile_path, monkeypatch):
    job_id = _job(conn, "already-drafted")
    worker.set_run_state(conn, "apply", status="running", mode="manual",
                         current_job_id=job_id)

    async def fail_if_called(*a, **kw):
        raise AssertionError("submit should not be called again")

    monkeypatch.setattr(worker.ats_apply, "submit", fail_if_called)
    await worker.apply_tick(conn, brief_path, profile_path)  # must not raise


async def test_tick_autopauses_on_daily_cap(conn, brief_path, profile_path):
    # daily_cap = 0 is rejected by CareerBrief validation (see
    # test_config.py::test_brief_rejects_zero_daily_cap), so the cap is
    # reached here the realistic way: daily_cap = 1 with one job already
    # submitted today, rather than the brief's literal (invalid) daily_cap
    # = 0 toml.
    p = brief_path.parent / "career_brief.toml"
    p.write_text(
        'target_titles = ["AI Engineer"]\n'
        'search_locations = ["Chennai"]\n'
        'daily_cap = 1\n')
    already_applied = _job(conn, "already-applied-today")
    conn.execute(
        "INSERT INTO application (job_id, resume_version, status,"
        " submitted_at) VALUES (?, 'v1', 'submitted', datetime('now'))",
        (already_applied,))
    conn.commit()
    _job(conn, "capped")
    worker.set_run_state(conn, "apply", status="running", mode="auto")

    await worker.apply_tick(conn, p, profile_path)

    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "paused"
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "run_autopaused" in types


async def test_tick_pauses_on_a_non_cap_denial(conn, brief_path, profile_path):
    # The reachable non-cap denial is a legacy event type='pause' row. The
    # job stays queued, so clearing current_job_id alone would re-pick it
    # every 0.1 s (two chat messages each time) -- pause instead.
    job_id = _job(conn, "gate-skipped")
    store.log(conn, None, "pause", "on")
    worker.set_run_state(conn, "apply", status="running", mode="auto")

    await worker.apply_tick(conn, brief_path, profile_path)

    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "paused"
    assert state["current_job_id"] is None
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "run_autopaused" in types
    assert any("auto-paused" in t for t in _chat_texts(conn, job_id))


async def test_apply_tick_expires_a_stale_card_before_tailoring(
        conn, brief_path, profile_path, monkeypatch):
    from career_agent import chat
    job_id = _job(conn, "fp1")
    chat.open_prompt(conn, job_id, "text", {"id": "needs_answer", "question": "PMP?",
                                            "origin": "needs_answer"})
    worker.set_run_state(conn, "apply", status="running", mode="manual")
    seen = []

    async def fake_tailor(conn, job_id, brief_path):
        seen.append(chat.open_prompt_for_job(conn, job_id))
        return "base-v1"

    async def fake_submit(conn, job_id, mode, **kw):
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(worker, "tailor_for_apply", fake_tailor)
    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)
    await worker.apply_tick(conn, brief_path, profile_path)
    assert seen == [None]


async def test_tick_errors_on_unhandled_submit_exception(
        conn, brief_path, profile_path, monkeypatch):
    _job(conn, "boom")
    worker.set_run_state(conn, "apply", status="running", mode="auto")

    async def boom(*a, **kw):
        raise RuntimeError("browser crashed")

    monkeypatch.setattr(worker.ats_apply, "submit", boom)
    await worker.apply_tick(conn, brief_path, profile_path)

    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "error"
    assert "browser crashed" in state["last_error"]
    assert state["current_job_id"] is None


async def test_loop_marks_run_errored_on_exception_apply_tick_doesnt_catch(
        conn, brief_path, profile_path, monkeypatch, tmp_path):
    """apply_tick only catches exceptions around its own submit() calls, so
    anything else that raises (e.g. next_candidate's query blowing up) must
    still be caught by the loop itself -- otherwise the fire-and-forget
    background task dies silently and /run/status keeps claiming 'running'
    forever with no last_error to explain why."""
    _job(conn, "boom-loop")
    worker.set_run_state(conn, "apply", status="running", mode="auto")

    def boom(*a, **kw):
        raise RuntimeError("candidate query exploded")

    monkeypatch.setattr(worker, "next_candidate", boom)

    task = asyncio.create_task(
        worker.apply_worker_loop(lambda: db.connect(tmp_path / "t.db"), brief_path, profile_path))
    for _ in range(50):
        await asyncio.sleep(0)
        if worker.get_run_state(conn, "apply")["status"] == "error":
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass  # expected: cancel() is how the caller stops the loop

    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "error"
    assert "candidate query exploded" in state["last_error"]
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "run_error" in types


async def test_apply_tick_hands_its_conn_factory_to_submit(
        conn, brief_path, profile_path, monkeypatch):
    """Without it the run's narration never reaches the job's chat -- a
    silent loss, so it is pinned here."""
    _job(conn, "fp1")
    conn.execute("INSERT INTO resume (version, path) VALUES ('base-v1', 'r.docx')")
    conn.commit()
    worker.set_run_state(conn, "apply", status="running", mode="manual")
    captured = {}

    async def fake_submit(conn, job_id, mode, conn_factory=None, **kw):
        captured["conn_factory"] = conn_factory
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)
    factory = lambda: conn
    await worker.apply_tick(conn, brief_path, profile_path, factory)
    assert captured["conn_factory"] is factory


# -- Task 9: lifecycle system messages ------------------------------------------

def _chat_texts(conn, job_id=None):
    from career_agent import chat
    cid = chat.home_conversation(conn) if job_id is None else chat.conversation_for_job(conn, job_id)
    return [m["content"] for m in chat.messages_after(conn, cid) if m["role"] == "system"]


async def test_apply_tick_posts_pick_up_and_outcome_messages(
        conn, brief_path, profile_path, monkeypatch):
    job_id = _job(conn, "fp1")
    worker.set_run_state(conn, "apply", status="running", mode="manual")

    async def fake_submit(conn, job_id, mode, **kw):
        conn.execute("INSERT INTO application (job_id, resume_version, status, failure_reason)"
                     " VALUES (?, 'v1', 'failed', 'stuck')", (job_id,))
        conn.commit()
        return {"ok": False, "reason": "failed: stuck"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)
    await worker.apply_tick(conn, brief_path, profile_path)
    texts = _chat_texts(conn, job_id)
    assert "Picked up by the apply worker (manual mode)" in texts
    assert any(t.startswith("Run ended") and "failed" in t and "stuck" in t for t in texts)


async def test_empty_queue_posts_idle_to_home(conn, brief_path, profile_path):
    worker.set_run_state(conn, "apply", status="running", mode="auto")
    await worker.apply_tick(conn, brief_path, profile_path)
    assert "Queue empty — apply run idle" in _chat_texts(conn)


async def test_unsupported_pause_posts_the_reason(conn, brief_path, profile_path, monkeypatch):
    job_id = _job(conn, "fp1")
    worker.set_run_state(conn, "apply", status="running", mode="auto")

    async def fake_submit(conn, job_id, mode, **kw):
        return {"ok": False, "unsupported": True, "reason": "claude not on PATH"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)
    await worker.apply_tick(conn, brief_path, profile_path)
    texts = _chat_texts(conn, job_id)
    assert any("paused" in t.lower() and "claude not on PATH" in t for t in texts)


async def test_a_chat_write_failure_does_not_change_the_tick(
        conn, brief_path, profile_path, monkeypatch):
    job_id = _job(conn, "fp1")
    worker.set_run_state(conn, "apply", status="running", mode="manual")

    async def fake_submit(conn, job_id, mode, **kw):
        return {"ok": True, "job_id": job_id, "status": "draft"}

    def broken_factory():
        raise RuntimeError("chat db down")

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)
    await worker.apply_tick(conn, brief_path, profile_path, broken_factory)
    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "running" and state["current_job_id"] is None   # a draft moves on


def test_startup_sweep_makes_orphaned_checkpoints_resumable(conn, monkeypatch):
    """A running checkpoint with no live run (a crashed server) becomes resumable;
    one a live run is still driving is left alone."""
    from career_agent.apply import agent as agent_mod
    from career_agent.apply import checkpoint

    a, b = _job(conn, "orphan"), _job(conn, "live")
    checkpoint.start(conn, a, "s1", "n", mode="manual", can_submit=True)
    checkpoint.mark_waiting(conn, a, 1)         # parked on a card: safe to resume
    checkpoint.start(conn, b, "s2", "n", mode="manual", can_submit=True)

    class Live:
        done = asyncio.Event()      # has .is_set() -> False
    monkeypatch.setattr(agent_mod, "RUNS", {b: Live()})
    worker.startup_sweep(conn)
    assert checkpoint.get(conn, a)["status"] == "resumable"
    assert checkpoint.get(conn, b)["status"] == "running"


async def test_the_loop_itself_never_sweeps(conn, brief_path, profile_path, monkeypatch, tmp_path):
    """Round 2 Minor 2: after boot a request-started submit can own an in_flight
    row that is not yet in RUNS; only app.lifespan sweeps."""
    from career_agent.apply import agent as agent_mod
    from career_agent.apply import checkpoint

    job_id = _job(conn, "starting")
    checkpoint.start(conn, job_id, "s", "n", mode="manual", can_submit=False)
    conn.execute("INSERT INTO application (job_id, resume_version, status, started_at)"
                 " VALUES (?, 'v', 'in_flight', datetime('now'))", (job_id,))
    conn.commit()
    monkeypatch.setattr(agent_mod, "RUNS", {})
    await _start_and_stop_loop(lambda: db.connect(tmp_path / "t.db"), brief_path, profile_path)
    assert conn.execute("SELECT status FROM application").fetchone()["status"] == "in_flight"
    assert checkpoint.get(conn, job_id)["status"] == "running"


# -- Task 11: auto-resume, resume cap, orphan in_flight ------------------------

def _resumable_job(conn, mode="auto", can_submit=True):
    from career_agent.apply import checkpoint
    job_id = _job(conn, f"resumable-{mode}")
    checkpoint.start(conn, job_id, "sess", "nonce", mode=mode, can_submit=can_submit)
    checkpoint.mark_resumable(conn, job_id)
    return job_id


def _recording_submit(calls, result=None):
    async def fake_submit(conn, job_id, **kw):
        calls.append({"job_id": job_id, **kw})
        return result or {"ok": False, "resumable": True, "reason": "stopped (timeout); resumable"}
    return fake_submit


async def test_auto_mode_resumes_a_resumable_checkpoint_once(conn, brief_path, profile_path, monkeypatch):
    job_id = _resumable_job(conn)
    calls = []
    monkeypatch.setattr(worker.ats_apply, "submit", _recording_submit(calls))
    worker.set_run_state(conn, "apply", status="running", mode="auto")
    await worker.apply_tick(conn, brief_path, profile_path)
    assert [(c["job_id"], c.get("resume"), c["mode"]) for c in calls] == [(job_id, True, "auto")]
    assert worker.get_run_state(conn, "apply")["current_job_id"] is None
    await worker.apply_tick(conn, brief_path, profile_path)     # still resumable: never again
    assert len(calls) == 1
    assert worker.get_run_state(conn, "apply")["status"] == "idle"
    assert any("where it left off" in t for t in _chat_texts(conn, job_id))


async def test_manual_mode_never_auto_resumes(conn, brief_path, profile_path, monkeypatch):
    _resumable_job(conn, mode="manual")
    calls = []
    monkeypatch.setattr(worker.ats_apply, "submit", _recording_submit(calls))
    worker.set_run_state(conn, "apply", status="running", mode="manual")
    await worker.apply_tick(conn, brief_path, profile_path)
    assert calls == [] and worker.get_run_state(conn, "apply")["status"] == "idle"


async def test_auto_resume_past_the_cap_records_a_retryable_failure(conn, brief_path, profile_path,
                                                                   monkeypatch):
    import functools
    from career_agent.apply import checkpoint
    from career_agent.apply.agent import AgentResult

    job_id = _resumable_job(conn)
    conn.execute("UPDATE apply_checkpoint SET resume_count = ?", (worker.ats_apply.MAX_RESUMES,))
    conn.commit()
    ran = []

    async def runner(prompt, jid, nonce, events, session_id=None, resume=False):
        ran.append(jid)
        return AgentResult("draft_ready")
    monkeypatch.setattr(worker.ats_apply, "submit",
                        functools.partial(worker.ats_apply.submit, run_agent=runner))
    worker.set_run_state(conn, "apply", status="running", mode="auto")
    await worker.apply_tick(conn, brief_path, profile_path)
    assert ran == []
    row = conn.execute("SELECT status, failure_reason FROM application WHERE job_id = ?",
                       (job_id,)).fetchone()
    assert (row["status"], row["failure_reason"]) == ("failed", "resume_limit")
    assert checkpoint.get(conn, job_id)["status"] == "done"
    assert worker.get_run_state(conn, "apply")["current_job_id"] is None


async def _start_and_stop_loop(factory, brief_path, profile_path):
    task = asyncio.create_task(worker.apply_worker_loop(factory, brief_path, profile_path))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.parametrize("implemented", [False, True])
def test_startup_sweep_drops_orphan_in_flight_rows_while_submission_is_off(
        conn, monkeypatch, implemented):
    from career_agent.apply import agent as agent_mod

    a, b = _job(conn, "orphan"), _job(conn, "live")
    for jid in (a, b):
        conn.execute("INSERT INTO application (job_id, resume_version, status, started_at)"
                     " VALUES (?, 'v', 'in_flight', datetime('now'))", (jid,))
    conn.commit()

    class Live:
        done = asyncio.Event()
    monkeypatch.setattr(agent_mod, "RUNS", {b: Live()})
    monkeypatch.setattr(worker.ats_apply, "SUBMISSION_IMPLEMENTED", implemented)
    worker.startup_sweep(conn)
    left = [r["job_id"] for r in conn.execute("SELECT job_id FROM application WHERE status = 'in_flight'")]
    dropped = [r["job_id"] for r in conn.execute("SELECT job_id FROM event WHERE type = 'orphan_in_flight_dropped'")]
    if implemented:
        assert sorted(left) == [a, b] and dropped == []
    else:
        assert left == [b] and dropped == [a]


async def test_auto_mode_never_resumes_a_manual_checkpoint(conn, brief_path, profile_path, monkeypatch):
    """I1: a manual stop (e.g. Apply anyway on a gate skip) restarted as auto
    would submit a job nobody reviewed."""
    _resumable_job(conn, mode="manual")
    calls = []
    monkeypatch.setattr(worker.ats_apply, "submit", _recording_submit(calls))
    worker.set_run_state(conn, "apply", status="running", mode="auto")
    await worker.apply_tick(conn, brief_path, profile_path)
    assert calls == [] and worker.get_run_state(conn, "apply")["status"] == "idle"


async def test_a_guard_denial_does_not_use_up_the_auto_resume(conn, brief_path, profile_path, monkeypatch):
    """I6."""
    from career_agent.apply import checkpoint

    job_id = _resumable_job(conn)
    calls = []
    monkeypatch.setattr(worker.ats_apply, "submit", _recording_submit(calls))
    store.log(conn, None, "pause", "on")
    worker.set_run_state(conn, "apply", status="running", mode="auto")
    await worker.apply_tick(conn, brief_path, profile_path)
    assert calls == [] and worker.get_run_state(conn, "apply")["status"] == "paused"
    assert checkpoint.get(conn, job_id)["auto_resumed"] == 0


def test_startup_sweep_drops_an_old_orphan_before_the_stale_sweep_holds_it(conn, monkeypatch):
    """I2: a restart 45 minutes after the crash, in the real order: startup_sweep,
    then the first _conn()'s sweep_stale_in_flight."""
    from career_agent.apply import agent as agent_mod
    from career_agent.apply import checkpoint

    monkeypatch.setattr(agent_mod, "RUNS", {})
    monkeypatch.setattr(worker.ats_apply, "SUBMISSION_IMPLEMENTED", False)
    job_id = _job(conn, "crashed")
    checkpoint.start(conn, job_id, "s", "n", mode="manual", can_submit=False)
    conn.execute("INSERT INTO application (job_id, resume_version, status, started_at)"
                 " VALUES (?, 'v', 'in_flight', datetime('now', '-45 minutes'))", (job_id,))
    conn.commit()
    worker.startup_sweep(conn)
    assert worker.ats_apply.sweep_stale_in_flight(conn) == 0
    assert conn.execute("SELECT COUNT(*) n FROM application").fetchone()["n"] == 0
    assert checkpoint.get(conn, job_id)["status"] == "resumable"


# -- final review ---------------------------------------------------------------

def _texts(conn, job_id):
    from career_agent import chat
    cid = chat.home_conversation(conn) if job_id is None else chat.conversation_for_job(conn, job_id)
    return [m["content"] for m in chat.messages_after(conn, cid)]


async def test_a_manual_worker_moves_on_after_a_draft(conn, brief_path, profile_path, monkeypatch):
    """I1: the in-session CONFIRM was the review; a draft does not park."""
    first, second = _job(conn, "draft-1", score=90), _job(conn, "draft-2", score=80)
    worker.set_run_state(conn, "apply", status="running", mode="manual")
    calls = []

    async def fake_submit(conn, job_id, mode, resume_version=None, **kw):
        calls.append(job_id)
        conn.execute("INSERT INTO application (job_id, resume_version, status)"
                     " VALUES (?, 'v1', 'draft')", (job_id,))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)
    await worker.apply_tick(conn, brief_path, profile_path)
    state = worker.get_run_state(conn, "apply")
    assert (state["status"], state["current_job_id"]) == ("running", None)
    await worker.apply_tick(conn, brief_path, profile_path)
    assert calls == [first, second]


def test_next_candidate_skips_a_job_with_an_apply_starting(conn):
    """M1: a job a request is tailoring or running is not the worker's to pick."""
    a, b = _job(conn, "pending", score=90), _job(conn, "free", score=80)
    worker.ats_apply.PENDING.add(a)
    try:
        assert worker.next_candidate(conn)["job_id"] == b
    finally:
        worker.ats_apply.PENDING.discard(a)


@pytest.mark.parametrize("implemented", [False, True])
def test_startup_sweep_tells_each_interrupted_job_and_home(conn, monkeypatch, implemented):
    """I3 (and I2's crash rule): a waiting manual session is resumable, a running
    can_submit one is held; each chat says so, and Home gets one summary."""
    from career_agent.apply import agent as agent_mod
    from career_agent.apply import checkpoint

    monkeypatch.setattr(agent_mod, "RUNS", {})
    monkeypatch.setattr(worker.ats_apply, "SUBMISSION_IMPLEMENTED", implemented)
    parked, busy = _job(conn, "parked"), _job(conn, "busy")
    checkpoint.start(conn, parked, "s1", "n", mode="manual", can_submit=True)
    checkpoint.mark_waiting(conn, parked, 7)
    checkpoint.start(conn, busy, "s2", "n", mode="manual", can_submit=True)
    for jid in (parked, busy):
        conn.execute("INSERT INTO application (job_id, resume_version, status, started_at)"
                     " VALUES (?, 'v', 'in_flight', datetime('now'))", (jid,))
    conn.commit()
    worker.startup_sweep(conn)
    assert checkpoint.get(conn, parked)["status"] == "resumable"
    assert checkpoint.get(conn, busy)["status"] == "done"
    left = {r["job_id"] for r in conn.execute("SELECT job_id FROM application WHERE status = 'in_flight'")}
    assert left == {busy}, "a can_submit row stays for adjudication, whatever the switch says now"
    assert any("press Continue" in t for t in _texts(conn, parked))
    assert any("held for review" in t for t in _texts(conn, busy))
    home = _texts(conn, None)
    assert any(f"2 job(s) interrupted by a restart: #{parked}, #{busy}" in t for t in home), home


def test_startup_sweep_says_nothing_when_nothing_was_interrupted(conn, monkeypatch):
    from career_agent.apply import agent as agent_mod
    monkeypatch.setattr(agent_mod, "RUNS", {})
    worker.startup_sweep(conn)
    assert not any("interrupted" in t for t in _texts(conn, None))


async def test_a_secret_needs_answer_does_not_park_the_worker(conn, brief_path, profile_path, monkeypatch):
    """Follow-up to C1: the real submit() records it as a failure, so the worker moves on."""
    import functools
    from career_agent import chat
    from career_agent.apply.agent import AgentResult

    first, second = _job(conn, "secret-q", score=90), _job(conn, "next", score=80)
    conn.execute("INSERT INTO resume (version, path) VALUES ('base-v1', 'r.docx')")
    conn.commit()
    seen = []

    async def runner(prompt, jid, nonce, events, session_id=None, resume=False):
        seen.append(jid)
        return AgentResult("needs_answer", "Your account password?")

    async def fake_tailor(conn, job_id, brief_path):
        return "base-v1"
    monkeypatch.setattr(worker, "tailor_for_apply", fake_tailor)
    monkeypatch.setattr(worker.ats_apply, "_stage_resume", lambda *a: "r.docx")
    monkeypatch.setattr(worker.ats_apply.Path, "exists", lambda self: True)
    monkeypatch.setattr(worker.ats_apply, "submit",
                        functools.partial(worker.ats_apply.submit, run_agent=runner))
    worker.set_run_state(conn, "apply", status="running", mode="manual")
    await worker.apply_tick(conn, brief_path, profile_path)
    state = worker.get_run_state(conn, "apply")
    assert (state["status"], state["current_job_id"]) == ("running", None)
    assert chat.open_prompt_for_job(conn, first) is None
    await worker.apply_tick(conn, brief_path, profile_path)
    assert seen == [first, second]


# -- final re-review -----------------------------------------------------------

async def test_a_request_claim_survives_a_tick_that_skips_or_picks_its_job(
        conn, brief_path, profile_path, monkeypatch):
    """M-B: the auto-resume pick skips a claimed job, and a tick releases only a
    claim it added."""
    from career_agent.apply import checkpoint
    job_id = _resumable_job(conn)
    calls = []
    monkeypatch.setattr(worker.ats_apply, "submit",
                        _recording_submit(calls, {"ok": True, "status": "draft"}))
    worker.set_run_state(conn, "apply", status="running", mode="auto")
    worker.ats_apply.PENDING.add(job_id)
    try:
        await worker.apply_tick(conn, brief_path, profile_path)
        assert calls == [] and job_id in worker.ats_apply.PENDING        # skipped
        worker.set_run_state(conn, "apply", status="running")
        monkeypatch.setattr(checkpoint, "next_auto_resume", lambda c, **k: None)
        monkeypatch.setattr(worker, "next_candidate", lambda c: {"job_id": job_id})
        await worker.apply_tick(conn, brief_path, profile_path)
        assert job_id in worker.ats_apply.PENDING, "a tick released a claim it did not add"
    finally:
        worker.ats_apply.PENDING.discard(job_id)


def test_startup_sweep_keeps_the_row_of_a_can_submit_crash_after_the_switch_is_off(conn, monkeypatch):
    """M-D: the checkpoint's recorded can_submit decides, not today's switch."""
    from career_agent.apply import agent as agent_mod
    from career_agent.apply import checkpoint
    monkeypatch.setattr(agent_mod, "RUNS", {})
    monkeypatch.setattr(worker.ats_apply, "SUBMISSION_IMPLEMENTED", False)
    job_id = _job(conn, "was-on")
    checkpoint.start(conn, job_id, "s", "n", mode="manual", can_submit=True)
    conn.execute("INSERT INTO application (job_id, resume_version, status, started_at)"
                 " VALUES (?, 'v', 'in_flight', datetime('now'))", (job_id,))
    conn.commit()
    worker.startup_sweep(conn)
    assert conn.execute("SELECT status FROM application WHERE job_id = ?",
                        (job_id,)).fetchone()["status"] == "in_flight"
    assert checkpoint.get(conn, job_id)["status"] == "done"


def test_startup_sweep_does_not_call_a_finished_job_held(conn, monkeypatch):
    """M-E: a lost finish() on a job that already drafted is no restart news."""
    from career_agent.apply import agent as agent_mod
    from career_agent.apply import checkpoint
    monkeypatch.setattr(agent_mod, "RUNS", {})
    job_id = _job(conn, "finished")
    checkpoint.start(conn, job_id, "s", "n", mode="manual", can_submit=False)
    conn.execute("INSERT INTO application (job_id, resume_version, status) VALUES (?, 'v', 'draft')",
                 (job_id,))
    conn.commit()
    worker.startup_sweep(conn)
    assert checkpoint.get(conn, job_id)["status"] == "done"
    said = _texts(conn, job_id) + _texts(conn, None)
    assert not any("held for review" in t or "interrupted" in t for t in said), said
