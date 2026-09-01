import sqlite3

import pytest

from career_agent import db, store
from career_agent.apply import ats as ats_apply
from career_agent.config import CareerBrief
from career_agent.models import Job, Verdict

BRIEF = CareerBrief(target_titles=["AI Engineer"], search_locations=["Chennai"],
                    locations=["Chennai"], staleness_days=30)

V = Verdict(role_fit=90, credibility=90, opportunity=90,
            application_quality=90, eligibility_soft=90,
            verdict="submit", rationale="ok")


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    return c


def _job(**kw):
    base = dict(source="ats", external_id="1", company="Acme",
                title="AI Engineer", location="Chennai")
    return Job(**{**base, **kw})


def test_upsert_inserts_new_and_skips_duplicate_fingerprints(conn):
    assert store.upsert_jobs(conn, [_job()], BRIEF) == 1
    assert store.upsert_jobs(conn, [_job(source="linkedin", external_id="9")],
                             BRIEF) == 0
    assert conn.execute("SELECT COUNT(*) n FROM job").fetchone()["n"] == 1


def test_upsert_drops_stale(conn):
    assert store.upsert_jobs(conn, [_job(posted_at="2019-01-01")], BRIEF) == 0


def test_unscored_excludes_current_version_but_not_old(conn):
    store.upsert_jobs(conn, [_job()], BRIEF)
    job_id = conn.execute("SELECT id FROM job").fetchone()["id"]
    assert len(store.unscored_jobs(conn, "gate-v1", 10)) == 1

    store.save_assessment(conn, job_id, V, "m", "gate-v1")
    assert store.unscored_jobs(conn, "gate-v1", 10) == []
    # bumping the prompt version invalidates the old assessment
    assert len(store.unscored_jobs(conn, "gate-v2", 10)) == 1


def test_unscored_excludes_hard_skipped(conn):
    store.upsert_jobs(conn, [_job()], BRIEF)
    job_id = conn.execute("SELECT id FROM job").fetchone()["id"]
    store.save_hard_skip(conn, job_id, "location outside accepted set")
    assert store.unscored_jobs(conn, "gate-v1", 10) == []


def test_unscored_excludes_merged_jobs(conn):
    store.upsert_jobs(conn, [_job(), _job(company="Globex")], BRIEF)
    a, b = [r["id"] for r in conn.execute("SELECT id FROM job ORDER BY id")]
    conn.execute("UPDATE job SET merged_into_job_id = ? WHERE id = ?", (a, b))
    assert len(store.unscored_jobs(conn, "gate-v1", 10)) == 1


def test_facts_returns_claims(conn):
    conn.execute("INSERT INTO fact (claim, evidence) VALUES ('Built RAG', 'proj X')")
    assert store.facts(conn) == ["Built RAG (evidence: proj X)"]


def test_settings_default_to_sonnet_and_25(conn):
    s = store.get_settings(conn)
    assert s["scoring_model"] == "claude-sonnet-5"
    assert s["max_score_per_run"] == 25


def test_save_settings_round_trips(conn):
    store.save_settings(conn, "claude-haiku-4-5", 50)
    s = store.get_settings(conn)
    assert s["scoring_model"] == "claude-haiku-4-5"
    assert s["max_score_per_run"] == 50


def test_save_settings_rejects_an_unknown_model(conn):
    with pytest.raises(ValueError, match="unknown scoring model"):
        store.save_settings(conn, "gpt-4", 25)
    assert store.get_settings(conn)["scoring_model"] == "claude-sonnet-5"


def test_save_settings_rejects_a_negative_cap(conn):
    with pytest.raises(ValueError):
        store.save_settings(conn, "claude-sonnet-5", -1)
    assert store.get_settings(conn)["max_score_per_run"] == 25


def test_save_settings_allows_zero_cap(conn):
    """0 is meaningful: discovery and the hard filter run, scoring does not."""
    store.save_settings(conn, "claude-sonnet-5", 0)
    assert store.get_settings(conn)["max_score_per_run"] == 0


def _seed_one(conn, company="Acme") -> int:
    store.upsert_jobs(conn, [_job(company=company)], BRIEF)
    return conn.execute("SELECT id FROM job WHERE company = ?",
                        (company,)).fetchone()["id"]


def _seed_facts(conn, n=10):
    for i in range(n):
        conn.execute("INSERT INTO fact (claim, evidence) VALUES (?, ?)",
                     (f"claim {i}", f"evidence {i}"))
    conn.commit()


def test_fact_rows_returns_ids_paired_with_formatted_text(conn):
    _seed_facts(conn, n=2)
    rows = store.fact_rows(conn)
    assert rows == [(1, "claim 0 (evidence: evidence 0)"),
                    (2, "claim 1 (evidence: evidence 1)")]


def test_latest_resume_version_is_none_with_no_rows(conn):
    job_id = _seed_one(conn)
    assert store.latest_resume_version(conn, job_id) is None


def test_latest_resume_version_returns_the_newest_row(conn):
    job_id = _seed_one(conn)
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES ('tailored-1-r1', 'a.docx', ?)", (job_id,))
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES ('tailored-1-r2', 'b.docx', ?)", (job_id,))
    conn.commit()
    assert store.latest_resume_version(conn, job_id) == "tailored-1-r2"


def test_resume_version_for_falls_back_to_the_constant(conn):
    job_id = _seed_one(conn)
    assert store.resume_version_for(conn, job_id) == ats_apply.RESUME_VERSION


def test_resume_version_for_prefers_a_tailored_row(conn):
    job_id = _seed_one(conn)
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES ('tailored-1-r1', 'a.docx', ?)", (job_id,))
    conn.commit()
    assert store.resume_version_for(conn, job_id) == "tailored-1-r1"


def test_next_resume_version_counts_existing_rows_for_that_job(conn):
    job_id = _seed_one(conn)
    assert store.next_resume_version(conn, job_id) == f"tailored-{job_id}-r1"
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES (?, 'a.docx', ?)",
                 (f"tailored-{job_id}-r1", job_id))
    conn.commit()
    assert store.next_resume_version(conn, job_id) == f"tailored-{job_id}-r2"


def test_insert_resume_stores_a_new_row(conn):
    job_id = _seed_one(conn)
    version = store.insert_resume(conn, job_id, "tailored-1-r1", "a.docx", "{}")
    assert version == "tailored-1-r1"
    row = conn.execute("SELECT * FROM resume WHERE version = ?",
                       (version,)).fetchone()
    assert row["job_id"] == job_id
    assert row["path"] == "a.docx"


def test_insert_resume_on_a_version_collision_returns_the_winner(conn):
    job_id = _seed_one(conn)
    store.insert_resume(conn, job_id, "tailored-1-r1", "a.docx", "{}")
    # A second insert under the same version string (the concurrent-click
    # race) must not raise -- it reports back the row that actually won.
    version = store.insert_resume(conn, job_id, "tailored-1-r1", "b.docx", "{}")
    assert version == "tailored-1-r1"
    assert conn.execute(
        "SELECT COUNT(*) n FROM resume WHERE version = ?",
        ("tailored-1-r1",)).fetchone()["n"] == 1


def test_mark_applied_promotes_an_existing_draft(conn):
    """The normal path: 'Open & track' left a draft, and the user then
    applied on the site. Promote that row rather than inserting a second,
    so one application attempt stays one row."""
    job_id = _seed_one(conn)
    conn.execute("INSERT INTO application (job_id, resume_version, status,"
                 " answers) VALUES (?, 'base-v1', 'draft', '{\"note\": \"x\"}')",
                 (job_id,))
    conn.commit()

    app_id = store.mark_applied(conn, job_id, "2026-08-20")

    rows = conn.execute("SELECT * FROM application WHERE job_id = ?",
                        (job_id,)).fetchall()
    assert len(rows) == 1, "promoted, not duplicated"
    assert rows[0]["id"] == app_id
    assert rows[0]["status"] == "submitted"
    assert rows[0]["submitted_at"] == "2026-08-20"
    assert rows[0]["answers"] is None, (
        "a manual application's contents are genuinely unknown; the draft's "
        "answers are precisely what was NOT sent, so NULL says so rather "
        "than inheriting the stub filler's placeholder")


def test_mark_applied_inserts_when_there_is_no_draft(conn):
    """Applying straight from the job board without tracking it first."""
    job_id = _seed_one(conn)

    app_id = store.mark_applied(conn, job_id, "2026-08-19")

    row = conn.execute("SELECT * FROM application WHERE id = ?",
                       (app_id,)).fetchone()
    assert row["status"] == "submitted"
    assert row["submitted_at"] == "2026-08-19"
    assert row["answers"] is None, (
        "a manual application's contents are genuinely unknown; NULL says so "
        "rather than inheriting the stub filler's placeholder")
    assert row["resume_version"] == ats_apply.RESUME_VERSION


def test_mark_applied_logs_the_human_decision(conn):
    job_id = _seed_one(conn)
    store.mark_applied(conn, job_id, "2026-08-20")
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "human_marked_applied" in types


def test_mark_applied_refuses_a_job_that_already_has_one(conn):
    """The partial unique index one_live_application_per_job makes double
    marking structurally impossible. Confirm it actually fires."""
    job_id = _seed_one(conn)
    store.mark_applied(conn, job_id, "2026-08-20")
    with pytest.raises(sqlite3.IntegrityError):
        store.mark_applied(conn, job_id, "2026-08-21")


def test_mark_applied_uses_the_tailored_resume_when_one_exists(conn):
    job_id = _seed_one(conn)
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES ('tailored-1-r1', 'x.docx', ?)", (job_id,))
    conn.commit()

    store.mark_applied(conn, job_id, "2026-08-22")

    row = conn.execute("SELECT resume_version FROM application"
                       " WHERE job_id = ?", (job_id,)).fetchone()
    assert row["resume_version"] == "tailored-1-r1"


def test_qa_normalize_collapses_punctuation_and_case(conn):
    assert store.qa_normalize("Notice period?") == store.qa_normalize("notice period")


def test_qa_lookup_returns_none_for_an_unseen_question(conn):
    assert store.qa_lookup(conn, "Notice period?") is None


def test_qa_upsert_then_lookup_round_trips(conn):
    store.qa_upsert(conn, "Notice period?", "30 days", is_volatile=True)
    row = store.qa_lookup(conn, "notice period")
    assert row["answer"] == "30 days"
    assert row["is_volatile"] == 1
    assert row["last_confirmed_at"] is not None


def test_qa_upsert_on_an_existing_question_overwrites_and_reconfirms(conn):
    store.qa_upsert(conn, "Notice period?", "30 days", is_volatile=True)
    first = store.qa_lookup(conn, "notice period")["last_confirmed_at"]
    store.qa_upsert(conn, "Notice period?", "60 days", is_volatile=True)
    row = store.qa_lookup(conn, "notice period")
    assert row["answer"] == "60 days"
    assert conn.execute("SELECT COUNT(*) n FROM qa_bank").fetchone()["n"] == 1


def test_application_has_taxonomy_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(application)")}
    assert {"failure_reason", "transcript_path"} <= cols


def test_qa_all_returns_every_row(conn):
    store.qa_upsert(conn, "Visa status?", "Citizen", is_volatile=True)
    store.qa_upsert(conn, "Years of Python?", "6", is_volatile=False)
    rows = store.qa_all(conn)
    assert {r["question_normalized"] for r in rows} == \
        {store.qa_normalize("Visa status?"),
         store.qa_normalize("Years of Python?")}
    # Verify all required fields are present
    for row in rows:
        assert "question_normalized" in row.keys()
        assert "answer" in row.keys()
        assert "is_volatile" in row.keys()
        assert "last_confirmed_at" in row.keys()
