import datetime as dt

import pytest

from career_agent import db, store
from career_agent.apply import ats as ats_apply


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    c.execute("INSERT INTO job (fingerprint, source, external_id, company,"
              " company_normalized, title, title_normalized, url)"
              " VALUES ('fp','ats','1','Acme','acme','AI Engineer',"
              " 'aiengineer','https://x/apply')")
    c.commit()
    return c


async def _ok(_url):
    return {"note": "filled"}


async def test_a_real_send_is_refused_while_the_filler_is_a_stub(conn):
    """No filler means the production path, where _default_filler fills
    nothing. Marking that 'submitted' would write a false row into the
    callback-rate denominator, which is the evidence the v1 -> v2 gate needs.
    Refuse before touching the database."""
    out = await ats_apply.submit(conn, 1, dry_run=False)
    assert out["ok"] is False
    assert "not implemented" in out["reason"].lower()
    assert conn.execute(
        "SELECT COUNT(*) n FROM application").fetchone()["n"] == 0


async def test_a_dry_run_is_still_allowed_on_the_stub(conn, monkeypatch):
    """Drafting still works -- it is how the dashboard records intent."""
    monkeypatch.setattr(ats_apply, "_default_filler", _ok)
    out = await ats_apply.submit(conn, 1, dry_run=True)
    assert out["ok"] is True
    assert conn.execute(
        "SELECT status FROM application").fetchone()["status"] == "draft"


async def test_an_injected_filler_is_still_allowed_to_send(conn):
    """The guard targets the stub, not real submission. Once a genuine filler
    exists it is passed in, and this path must not be blocked by the flag."""
    out = await ats_apply.submit(conn, 1, dry_run=False, filler=_ok)
    assert out["ok"] is True
    assert conn.execute(
        "SELECT status FROM application").fetchone()["status"] == "submitted"


async def test_dry_run_records_a_draft_and_does_not_send(conn):
    out = await ats_apply.submit(conn, 1, dry_run=True, filler=_ok)
    assert out["ok"] is True
    row = conn.execute("SELECT * FROM application WHERE job_id = 1").fetchone()
    assert row["status"] == "draft"
    assert row["submitted_at"] is None


async def test_a_draft_does_not_block_a_real_submission(conn):
    await ats_apply.submit(conn, 1, dry_run=True, filler=_ok)
    out = await ats_apply.submit(conn, 1, dry_run=False, filler=_ok)
    assert out["ok"] is True
    statuses = {r["status"] for r in conn.execute(
        "SELECT status FROM application WHERE job_id = 1")}
    assert statuses == {"draft", "submitted"}


async def test_second_real_submission_is_refused(conn):
    await ats_apply.submit(conn, 1, dry_run=False, filler=_ok)
    out = await ats_apply.submit(conn, 1, dry_run=False, filler=_ok)
    assert out["ok"] is False
    assert "already" in out["reason"]
    assert conn.execute(
        "SELECT COUNT(*) n FROM application WHERE job_id = 1").fetchone()["n"] == 1


async def test_captcha_holds_and_records_no_application(conn):
    async def boom(_url):
        raise ats_apply.CaptchaEncountered("recaptcha frame present")

    out = await ats_apply.submit(conn, 1, dry_run=False, filler=boom)
    assert out["held"] is True
    assert conn.execute("SELECT COUNT(*) n FROM application").fetchone()["n"] == 0
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "captcha_held" in types


async def test_three_failures_become_failed_permanent(conn):
    async def fail(_url):
        raise RuntimeError("form error")

    for _ in range(ats_apply.MAX_ATTEMPTS):
        out = await ats_apply.submit(conn, 1, dry_run=False, filler=fail)
        assert out["ok"] is False

    statuses = [r["status"] for r in conn.execute(
        "SELECT status FROM application WHERE job_id = 1 ORDER BY id")]
    assert statuses[-1] == "failed_permanent"

    out = await ats_apply.submit(conn, 1, dry_run=False, filler=_ok)
    assert out["ok"] is False
    assert conn.execute(
        "SELECT COUNT(*) n FROM application WHERE job_id = 1"
    ).fetchone()["n"] == ats_apply.MAX_ATTEMPTS


def test_stale_in_flight_becomes_held_unknown(conn):
    conn.execute("INSERT INTO application (job_id, resume_version, status,"
                 " started_at) VALUES (1, 'v1', 'in_flight',"
                 " datetime('now', '-30 minutes'))")
    conn.commit()
    assert ats_apply.sweep_stale_in_flight(conn, minutes=15) == 1
    row = conn.execute("SELECT status FROM application").fetchone()
    assert row["status"] == "held_unknown"


async def test_dry_run_stores_the_given_resume_version(conn):
    out = await ats_apply.submit(conn, 1, dry_run=True, filler=_ok,
                                 resume_version="tailored-1-r1")
    assert out["ok"] is True
    row = conn.execute("SELECT resume_version FROM application").fetchone()
    assert row["resume_version"] == "tailored-1-r1"


async def test_dry_run_falls_back_to_the_default_version(conn):
    out = await ats_apply.submit(conn, 1, dry_run=True, filler=_ok)
    assert out["ok"] is True
    row = conn.execute("SELECT resume_version FROM application").fetchone()
    assert row["resume_version"] == ats_apply.RESUME_VERSION


async def test_real_submission_stores_the_given_resume_version(conn):
    out = await ats_apply.submit(conn, 1, dry_run=False, filler=_ok,
                                 resume_version="tailored-1-r1")
    assert out["ok"] is True
    row = conn.execute("SELECT resume_version FROM application").fetchone()
    assert row["resume_version"] == "tailored-1-r1"


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
