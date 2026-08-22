import sqlite3

import pytest

from career_agent import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    return c


def _job(conn, fp="fp1"):
    return conn.execute(
        "INSERT INTO job (fingerprint, source, external_id, company,"
        " company_normalized, title, title_normalized)"
        " VALUES (?, 'ats', '1', 'Acme', 'acme', 'AI Engineer', 'aiengineer')",
        (fp,)).lastrowid


def test_schema_creates_expected_tables(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"job", "assessment", "application", "outcome",
            "event", "fact", "qa_bank", "resume"} <= names


def test_no_career_brief_table(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "career_brief" not in names


def test_draft_does_not_block_a_real_submission(conn):
    j = _job(conn)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (?, 'v1', 'draft')", (j,))
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (?, 'v1', 'in_flight')", (j,))


def test_failed_does_not_block_a_retry(conn):
    j = _job(conn)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (?, 'v1', 'failed')", (j,))
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (?, 'v1', 'in_flight')", (j,))


@pytest.mark.parametrize("blocking", ["in_flight", "submitted",
                                      "held_unknown", "failed_permanent"])
def test_live_statuses_block_a_second_attempt(conn, blocking):
    j = _job(conn)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (?, 'v1', ?)", (j, blocking))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO application (job_id, resume_version, status)"
                     " VALUES (?, 'v1', 'in_flight')", (j,))


def test_merged_job_is_soft_deleted_not_removed(conn):
    survivor = _job(conn, "fpA")
    dupe = _job(conn, "fpB")
    conn.execute("UPDATE job SET merged_into_job_id = ? WHERE id = ?",
                 (survivor, dupe))
    assert conn.execute("SELECT COUNT(*) n FROM job").fetchone()["n"] == 2
    live = conn.execute("SELECT COUNT(*) n FROM job"
                        " WHERE merged_into_job_id IS NULL").fetchone()["n"]
    assert live == 1


def test_run_state_seeded_with_apply_and_pipeline_rows(conn):
    kinds = {r["kind"] for r in conn.execute("SELECT kind FROM run_state")}
    assert kinds == {"apply", "pipeline"}


def test_run_state_apply_row_starts_idle_manual(conn):
    row = conn.execute(
        "SELECT status, mode FROM run_state WHERE kind = 'apply'").fetchone()
    assert row["status"] == "idle"
    assert row["mode"] == "manual"


def test_init_schema_is_idempotent_for_run_state(conn):
    db.init_schema(conn)  # called a second time by the fixture's next call
    count = conn.execute("SELECT COUNT(*) n FROM run_state").fetchone()["n"]
    assert count == 2


def test_job_has_priority_column(conn):
    j = _job(conn)
    conn.execute("UPDATE job SET priority = 3 WHERE id = ?", (j,))
    row = conn.execute("SELECT priority FROM job WHERE id = ?", (j,)).fetchone()
    assert row["priority"] == 3


def test_job_priority_defaults_to_null(conn):
    j = _job(conn)
    row = conn.execute("SELECT priority FROM job WHERE id = ?", (j,)).fetchone()
    assert row["priority"] is None


def test_resume_has_job_id_and_content_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(resume)")}
    assert {"job_id", "content"} <= cols


def test_resume_columns_are_added_idempotently(conn):
    db.init_schema(conn)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(resume)")]
    assert cols.count("job_id") == 1
    assert cols.count("content") == 1


def test_setting_row_is_seeded(conn):
    row = conn.execute("SELECT * FROM setting").fetchone()
    assert row["scoring_model"] == "claude-sonnet-5"
    assert row["max_score_per_run"] == 25


def test_setting_seeding_does_not_clobber_a_saved_choice(conn):
    """init_schema runs on every web request; re-seeding must not reset
    the user's model choice back to the default."""
    conn.execute("UPDATE setting SET scoring_model = 'claude-haiku-4-5',"
                 " max_score_per_run = 50")
    conn.commit()
    db.init_schema(conn)
    assert conn.execute("SELECT COUNT(*) n FROM setting").fetchone()["n"] == 1
    row = conn.execute("SELECT * FROM setting").fetchone()
    assert row["scoring_model"] == "claude-haiku-4-5"
    assert row["max_score_per_run"] == 50


def test_run_state_has_progress_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(run_state)")}
    assert {"stage", "found", "duplicates", "passed", "scored",
            "shortlisted"} <= cols


def test_progress_counters_default_to_zero(conn):
    row = conn.execute(
        "SELECT stage, found, duplicates, passed, scored, shortlisted"
        " FROM run_state WHERE kind = 'pipeline'").fetchone()
    assert row["stage"] is None
    assert (row["found"], row["duplicates"], row["passed"],
            row["scored"], row["shortlisted"]) == (0, 0, 0, 0, 0)


def test_progress_columns_are_added_idempotently(conn):
    db.init_schema(conn)
    db.init_schema(conn)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(run_state)")]
    assert cols.count("stage") == 1
    assert cols.count("found") == 1
