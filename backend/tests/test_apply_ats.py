import pytest

from career_agent import db, store
from career_agent.apply import ats as ats_apply
from career_agent.config import CandidateProfile


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


async def _compute_ok(url, job, brief, profile, conn):
    return {"note": "filled"}


async def _fill_ok(url, answers, resume_path):
    pass


def test_split_name_handles_a_single_and_a_multi_word_name():
    assert ats_apply._split_name("Jane") == ("Jane", "")
    assert ats_apply._split_name("Jane Van Doe") == ("Jane", "Van Doe")


def test_resolve_answers_uses_a_qa_bank_hit(conn):
    store.qa_upsert(conn, "Why this company?", "Great mission fit", is_volatile=False)
    questions = [ats_apply.FormField(label="Why this company?", locator="#q1")]
    answers = ats_apply.resolve_answers(questions, conn)
    assert answers == {"#q1": "Great mission fit"}


def test_resolve_answers_raises_needs_answer_with_no_qa_bank_entry(conn):
    questions = [ats_apply.FormField(label="Notice period?", locator="#q1")]
    with pytest.raises(ats_apply.NeedsAnswer) as exc:
        ats_apply.resolve_answers(questions, conn)
    assert exc.value.question == "Notice period?"


def test_resolve_answers_uses_a_fresh_volatile_answer(conn):
    store.qa_upsert(conn, "Current CTC?", "12 LPA", is_volatile=True)
    questions = [ats_apply.FormField(label="Current CTC?", locator="#q1")]
    answers = ats_apply.resolve_answers(questions, conn)
    assert answers == {"#q1": "12 LPA"}


def test_resolve_answers_treats_a_stale_volatile_answer_as_missing(conn):
    store.qa_upsert(conn, "Current CTC?", "12 LPA", is_volatile=True)
    conn.execute("UPDATE qa_bank SET last_confirmed_at = datetime('now', '-31 days')"
                 " WHERE question_normalized = ?", (store.qa_normalize("Current CTC?"),))
    conn.commit()
    questions = [ats_apply.FormField(label="Current CTC?", locator="#q1")]
    with pytest.raises(ats_apply.NeedsAnswer):
        ats_apply.resolve_answers(questions, conn)


def test_resolve_answers_accepts_a_volatile_answer_confirmed_29_days_ago(conn):
    store.qa_upsert(conn, "Current CTC?", "12 LPA", is_volatile=True)
    conn.execute("UPDATE qa_bank SET last_confirmed_at = datetime('now', '-29 days')"
                 " WHERE question_normalized = ?", (store.qa_normalize("Current CTC?"),))
    conn.commit()
    questions = [ats_apply.FormField(label="Current CTC?", locator="#q1")]
    answers = ats_apply.resolve_answers(questions, conn)
    assert answers == {"#q1": "12 LPA"}


async def test_a_real_send_on_a_greenhouse_job_is_refused_while_the_flag_is_off(conn):
    """SUBMISSION_IMPLEMENTED is False by default -- see the plan's Global
    Constraints. This is the regression guard: a future change must not
    silently flip real sends on."""
    out = await ats_apply.submit(conn, 1, dry_run=False, profile=PROFILE)
    assert out["ok"] is False
    assert out["unsupported"] is True
    assert conn.execute("SELECT COUNT(*) n FROM application").fetchone()["n"] == 0


async def test_a_real_send_on_a_non_greenhouse_job_is_refused_even_with_the_flag_on(
        conn, monkeypatch):
    conn.execute("UPDATE job SET source = 'linkedin' WHERE id = 1")
    conn.commit()
    monkeypatch.setattr(ats_apply, "SUBMISSION_IMPLEMENTED", True)
    out = await ats_apply.submit(conn, 1, dry_run=False, profile=PROFILE)
    assert out["ok"] is False
    assert out["unsupported"] is True


async def test_an_injected_fill_and_submit_bypasses_both_gates(conn):
    """The gates target the built-in defaults, not real submission in
    general -- an explicitly injected fill_and_submit is always allowed
    through, same as today's injected-filler escape hatch."""
    out = await ats_apply.submit(conn, 1, dry_run=True,
                                 compute_answers=_compute_ok)
    assert out["ok"] is True
    out = await ats_apply.submit(conn, 1, dry_run=False,
                                 fill_and_submit=_fill_ok)
    assert out["ok"] is True
    assert conn.execute(
        "SELECT status FROM application ORDER BY id DESC LIMIT 1"
    ).fetchone()["status"] == "submitted"


async def test_dry_run_routes_to_the_real_greenhouse_filler_by_default(
        conn, monkeypatch):
    """No compute_answers override, job.source == 'ats': submit() must pick
    _default_compute_answers on its own. Monkeypatches the module-level
    default rather than letting it run, so this never touches Playwright."""
    called = {}

    async def fake_default(url, job, brief, profile, conn):
        called["which"] = "greenhouse"
        return {}

    monkeypatch.setattr(ats_apply, "_default_compute_answers", fake_default)
    out = await ats_apply.submit(conn, 1, dry_run=True, profile=PROFILE)
    assert out["ok"] is True
    assert called["which"] == "greenhouse"


async def test_dry_run_routes_to_the_generic_stub_for_non_ats_jobs(
        conn, monkeypatch):
    conn.execute("UPDATE job SET source = 'linkedin' WHERE id = 1")
    conn.commit()
    called = {}

    async def fake_stub(url, job, brief, profile, conn):
        called["which"] = "stub"
        return {}

    monkeypatch.setattr(ats_apply, "_generic_stub_compute_answers", fake_stub)
    out = await ats_apply.submit(conn, 1, dry_run=True, profile=PROFILE)
    assert out["ok"] is True
    assert called["which"] == "stub"


async def test_dry_run_records_a_draft_and_does_not_send(conn):
    out = await ats_apply.submit(conn, 1, dry_run=True, compute_answers=_compute_ok)
    assert out["ok"] is True
    row = conn.execute("SELECT * FROM application WHERE job_id = 1").fetchone()
    assert row["status"] == "draft"
    assert row["submitted_at"] is None


async def test_a_real_send_reuses_the_drafts_answers_without_recomputing(conn):
    calls = []

    async def counting_compute(url, job, brief, profile, conn):
        calls.append(1)
        return {"#q1": "yes"}

    captured = {}

    async def capturing_fill(url, answers, resume_path):
        captured["answers"] = answers

    await ats_apply.submit(conn, 1, dry_run=True, compute_answers=counting_compute)
    await ats_apply.submit(conn, 1, dry_run=False, fill_and_submit=capturing_fill)

    assert len(calls) == 1, "compute_answers must not run a second time on the real send"
    assert captured["answers"] == {"#q1": "yes"}


async def test_a_draft_does_not_block_a_real_submission(conn):
    await ats_apply.submit(conn, 1, dry_run=True, compute_answers=_compute_ok)
    out = await ats_apply.submit(conn, 1, dry_run=False, fill_and_submit=_fill_ok)
    assert out["ok"] is True
    statuses = {r["status"] for r in conn.execute(
        "SELECT status FROM application WHERE job_id = 1")}
    assert statuses == {"draft", "submitted"}


async def test_second_real_submission_is_refused(conn):
    await ats_apply.submit(conn, 1, dry_run=False, fill_and_submit=_fill_ok)
    out = await ats_apply.submit(conn, 1, dry_run=False, fill_and_submit=_fill_ok)
    assert out["ok"] is False
    assert "already" in out["reason"]
    assert conn.execute(
        "SELECT COUNT(*) n FROM application WHERE job_id = 1").fetchone()["n"] == 1


async def test_captcha_during_draft_holds_and_records_no_application(conn):
    async def boom(url, job, brief, profile, conn):
        raise ats_apply.CaptchaEncountered("recaptcha frame present")

    out = await ats_apply.submit(conn, 1, dry_run=True, compute_answers=boom)
    assert out["held"] is True
    assert conn.execute("SELECT COUNT(*) n FROM application").fetchone()["n"] == 0
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "captcha_held" in types


async def test_captcha_during_real_send_holds_and_removes_the_in_flight_row(conn):
    await ats_apply.submit(conn, 1, dry_run=True, compute_answers=_compute_ok)

    async def boom(url, answers, resume_path):
        raise ats_apply.CaptchaEncountered("recaptcha frame present")

    out = await ats_apply.submit(conn, 1, dry_run=False, fill_and_submit=boom)
    assert out["held"] is True
    statuses = [r["status"] for r in conn.execute(
        "SELECT status FROM application WHERE job_id = 1")]
    assert statuses == ["draft"]  # the in_flight attempt was removed


async def test_needs_answer_is_reported_and_records_no_application(conn):
    async def needs(url, job, brief, profile, conn):
        raise ats_apply.NeedsAnswer("Notice period?")

    out = await ats_apply.submit(conn, 1, dry_run=True, compute_answers=needs)
    assert out["needs_answer"] == "Notice period?"
    assert conn.execute("SELECT COUNT(*) n FROM application").fetchone()["n"] == 0


async def test_three_failures_become_failed_permanent(conn):
    await ats_apply.submit(conn, 1, dry_run=True, compute_answers=_compute_ok)

    async def fail(url, answers, resume_path):
        raise RuntimeError("form error")

    for _ in range(ats_apply.MAX_ATTEMPTS):
        out = await ats_apply.submit(conn, 1, dry_run=False, fill_and_submit=fail)
        assert out["ok"] is False

    statuses = [r["status"] for r in conn.execute(
        "SELECT status FROM application WHERE job_id = 1 ORDER BY id")]
    assert statuses[-1] == "failed_permanent"

    out = await ats_apply.submit(conn, 1, dry_run=False, fill_and_submit=_fill_ok)
    assert out["ok"] is False


def test_stale_in_flight_becomes_held_unknown(conn):
    conn.execute("INSERT INTO application (job_id, resume_version, status,"
                 " started_at) VALUES (1, 'v1', 'in_flight',"
                 " datetime('now', '-30 minutes'))")
    conn.commit()
    assert ats_apply.sweep_stale_in_flight(conn, minutes=15) == 1
    row = conn.execute("SELECT status FROM application").fetchone()
    assert row["status"] == "held_unknown"


async def test_dry_run_stores_the_given_resume_version(conn):
    out = await ats_apply.submit(conn, 1, dry_run=True, compute_answers=_compute_ok,
                                 resume_version="tailored-1-r1")
    assert out["ok"] is True
    row = conn.execute("SELECT resume_version FROM application").fetchone()
    assert row["resume_version"] == "tailored-1-r1"


async def test_dry_run_falls_back_to_the_default_version(conn):
    out = await ats_apply.submit(conn, 1, dry_run=True, compute_answers=_compute_ok)
    assert out["ok"] is True
    row = conn.execute("SELECT resume_version FROM application").fetchone()
    assert row["resume_version"] == ats_apply.RESUME_VERSION


async def test_real_submission_refuses_when_no_resume_is_on_record(conn):
    out = await ats_apply.submit(conn, 1, dry_run=False, fill_and_submit=_fill_ok,
                                 resume_version="tailored-1-r1")
    assert out["ok"] is False
    assert "résumé" in out["reason"] or "resume" in out["reason"].lower()
