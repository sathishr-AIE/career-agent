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
