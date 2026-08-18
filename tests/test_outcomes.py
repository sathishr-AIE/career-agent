import pytest

from career_agent import db, outcomes


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    c.execute("INSERT INTO job (fingerprint, source, external_id, company,"
              " company_normalized, title, title_normalized)"
              " VALUES ('fp','ats','1','Acme','acme','AI Eng','aieng')")
    c.commit()
    return c


def _app(conn, days_ago):
    return conn.execute(
        "INSERT INTO application (job_id, resume_version, status, submitted_at)"
        f" VALUES (1, 'v1', 'submitted', datetime('now', '-{days_ago} days'))"
    ).lastrowid


def test_derives_no_response_after_the_window(conn):
    _app(conn, 31)
    assert outcomes.derive_no_response(conn, after_days=30) == 1
    row = conn.execute("SELECT type, derived FROM outcome").fetchone()
    assert row["type"] == "no_response"
    assert row["derived"] == 1


def test_does_not_derive_before_the_window(conn):
    _app(conn, 10)
    assert outcomes.derive_no_response(conn, after_days=30) == 0


def test_does_not_derive_at_the_boundary(conn):
    _app(conn, 30)
    assert outcomes.derive_no_response(conn, after_days=30) == 0
    assert conn.execute("SELECT COUNT(*) c FROM outcome").fetchone()["c"] == 0


def test_does_not_derive_twice(conn):
    _app(conn, 31)
    outcomes.derive_no_response(conn, after_days=30)
    assert outcomes.derive_no_response(conn, after_days=30) == 0


def test_does_not_derive_when_a_real_outcome_exists(conn):
    a = _app(conn, 31)
    conn.execute("INSERT INTO outcome (application_id, type, derived)"
                 " VALUES (?, 'rejected', 0)", (a,))
    conn.commit()
    assert outcomes.derive_no_response(conn, after_days=30) == 0


def test_manual_outcome_supersedes_a_derived_one(conn):
    a = _app(conn, 31)
    outcomes.derive_no_response(conn, after_days=30)
    conn.execute("INSERT INTO outcome (application_id, type, derived, occurred_at)"
                 " VALUES (?, 'screen', 0, datetime('now', '+1 day'))", (a,))
    conn.commit()
    assert outcomes.effective_outcome(conn, a) == "screen"


def test_derived_loses_a_tie(conn):
    a = _app(conn, 31)
    conn.execute("INSERT INTO outcome (application_id, type, derived, occurred_at)"
                 " VALUES (?, 'no_response', 1, '2026-08-01T00:00:00')", (a,))
    conn.execute("INSERT INTO outcome (application_id, type, derived, occurred_at)"
                 " VALUES (?, 'interview', 0, '2026-08-01T00:00:00')", (a,))
    conn.commit()
    assert outcomes.effective_outcome(conn, a) == "interview"


def test_callback_rate_counts_all_submitted_as_denominator(conn):
    a1 = _app(conn, 31)
    conn.execute("INSERT INTO job (fingerprint, source, external_id, company,"
                 " company_normalized, title, title_normalized)"
                 " VALUES ('fp2','ats','2','B','b','AI','ai')")
    a2 = conn.execute(
        "INSERT INTO application (job_id, resume_version, status, submitted_at)"
        " VALUES (2, 'v1', 'submitted', datetime('now'))").lastrowid
    conn.execute("INSERT INTO outcome (application_id, type) VALUES (?, 'screen')",
                 (a2,))
    conn.commit()
    outcomes.derive_no_response(conn, after_days=30)
    assert outcomes.callback_rate(conn) == (1, 2)
