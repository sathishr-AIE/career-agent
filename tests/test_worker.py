import asyncio

import pytest

from career_agent import db, store
from career_agent.web import worker


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    return c


@pytest.fixture
def brief_path(tmp_path):
    p = tmp_path / "career_brief.toml"
    p.write_text(
        'target_titles = ["AI Engineer"]\n'
        'search_locations = ["Chennai"]\n'
        'daily_cap = 5\n')
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


async def test_tick_does_nothing_when_not_running(conn, brief_path):
    job_id = _job(conn, "idle-test")
    await worker.apply_tick(conn, brief_path)
    assert worker.get_run_state(conn, "apply")["current_job_id"] is None
    assert conn.execute("SELECT COUNT(*) n FROM application").fetchone()["n"] == 0


async def test_tick_with_empty_queue_goes_idle_and_logs_completion(conn, brief_path):
    worker.set_run_state(conn, "apply", status="running", mode="auto")
    await worker.apply_tick(conn, brief_path)
    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "idle"
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "run_completed" in types


async def test_tick_auto_mode_drafts_and_sends(conn, brief_path, monkeypatch):
    job_id = _job(conn, "auto-me")
    worker.set_run_state(conn, "apply", status="running", mode="auto")

    calls = []

    async def fake_submit(conn, job_id, dry_run, filler=None):
        calls.append(dry_run)
        status = "draft" if dry_run else "submitted"
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', ?)", (job_id, status))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": status}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)
    await worker.apply_tick(conn, brief_path)

    assert calls == [True, False]
    assert worker.get_run_state(conn, "apply")["current_job_id"] is None


async def test_tick_auto_mode_skips_when_the_real_send_reports_not_ok(
        conn, brief_path, monkeypatch):
    """A captcha hold or permanent failure comes back as ok=False, not as an
    exception -- treating that as success would leave the run claiming it
    applied and, worse, silently move on with no job_skipped record."""
    job_id = _job(conn, "captcha")
    worker.set_run_state(conn, "apply", status="running", mode="auto")

    async def fake_submit(conn, job_id, dry_run, filler=None):
        if dry_run:
            conn.execute("INSERT INTO application (job_id, resume_version,"
                         " status) VALUES (?, 'v1', 'draft')", (job_id,))
            conn.commit()
            return {"ok": True, "job_id": job_id, "status": "draft"}
        return {"ok": False, "reason": "captcha held the submission"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)
    await worker.apply_tick(conn, brief_path)

    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "running"
    assert state["current_job_id"] is None
    skips = conn.execute(
        "SELECT payload FROM event WHERE type = 'job_skipped'").fetchall()
    assert [s["payload"] for s in skips] == ["captcha held the submission"]


async def test_tick_manual_mode_stops_after_draft(conn, brief_path, monkeypatch):
    job_id = _job(conn, "manual-me")
    worker.set_run_state(conn, "apply", status="running", mode="manual")

    calls = []

    async def fake_submit(conn, job_id, dry_run, filler=None):
        calls.append(dry_run)
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'draft')", (job_id,))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)
    await worker.apply_tick(conn, brief_path)

    assert calls == [True]
    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "running"
    assert state["current_job_id"] == job_id


async def test_tick_is_a_noop_while_awaiting_manual_review(conn, brief_path, monkeypatch):
    job_id = _job(conn, "already-drafted")
    worker.set_run_state(conn, "apply", status="running", mode="manual",
                         current_job_id=job_id)

    async def fail_if_called(*a, **kw):
        raise AssertionError("submit should not be called again")

    monkeypatch.setattr(worker.ats_apply, "submit", fail_if_called)
    await worker.apply_tick(conn, brief_path)  # must not raise


async def test_tick_autopauses_on_daily_cap(conn, brief_path):
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

    await worker.apply_tick(conn, p)

    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "paused"
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "run_autopaused" in types


async def test_tick_skips_a_denied_job_and_stays_running(conn, brief_path):
    # A verdict='skip' job is excluded by next_candidate's own SQL
    # (verdict IN ('submit','hold')), so it never reaches guard() through
    # apply_tick and can't exercise the job_skipped branch. Use a real
    # ('submit') candidate with the agent paused instead, the reachable
    # non-cap denial guard() produces.
    _job(conn, "gate-skipped")
    store.log(conn, None, "pause", "on")
    worker.set_run_state(conn, "apply", status="running", mode="auto")

    await worker.apply_tick(conn, brief_path)

    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "running"
    assert state["current_job_id"] is None
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "job_skipped" in types


async def test_tick_errors_on_unhandled_submit_exception(conn, brief_path, monkeypatch):
    _job(conn, "boom")
    worker.set_run_state(conn, "apply", status="running", mode="auto")

    async def boom(*a, **kw):
        raise RuntimeError("browser crashed")

    monkeypatch.setattr(worker.ats_apply, "submit", boom)
    await worker.apply_tick(conn, brief_path)

    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "error"
    assert "browser crashed" in state["last_error"]
    assert state["current_job_id"] is None


async def test_loop_marks_run_errored_on_exception_apply_tick_doesnt_catch(
        conn, brief_path, monkeypatch):
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

    task = asyncio.create_task(worker.apply_worker_loop(lambda: conn, brief_path))
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
