import pytest

from career_agent import db
from career_agent.web import worker


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    return c


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
