import pytest

from career_agent import db, store
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
