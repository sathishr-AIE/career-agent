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
    assert rows[0]["answers"] == '{"note": "x"}', "draft's record preserved"


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
