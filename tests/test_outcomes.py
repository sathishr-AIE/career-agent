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
    cutoff = conn.execute("SELECT datetime('now', '-30 days')").fetchone()[0]
    conn.execute(
        "INSERT INTO application (job_id, resume_version, status, submitted_at)"
        " VALUES (1, 'v1', 'submitted', ?)", (cutoff,))
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


def test_record_writes_a_manual_outcome(conn):
    app_id = _app(conn, 3)
    out_id = outcomes.record(conn, app_id, "screen", "2026-08-20")
    row = conn.execute("SELECT * FROM outcome WHERE id = ?",
                       (out_id,)).fetchone()
    assert row["type"] == "screen"
    assert row["derived"] == 0, "hand-entered, not inferred"
    assert row["occurred_at"] == "2026-08-20"


def test_record_stores_notes_when_given(conn):
    app_id = _app(conn, 3)
    out_id = outcomes.record(conn, app_id, "interview", "2026-08-20",
                             notes="30 min with the hiring manager")
    row = conn.execute("SELECT notes FROM outcome WHERE id = ?",
                       (out_id,)).fetchone()
    assert row["notes"] == "30 min with the hiring manager"


def test_record_refuses_no_response(conn):
    """Derived, never entered. The schema CHECK would accept it, so the
    write path has to be the thing that refuses."""
    app_id = _app(conn, 3)
    with pytest.raises(ValueError, match="derived"):
        outcomes.record(conn, app_id, "no_response", "2026-08-20")
    assert conn.execute("SELECT COUNT(*) n FROM outcome").fetchone()["n"] == 0


def test_record_refuses_an_unknown_type(conn):
    app_id = _app(conn, 3)
    with pytest.raises(ValueError):
        outcomes.record(conn, app_id, "ghosted", "2026-08-20")


def test_a_later_manual_outcome_supersedes_an_earlier_one(conn):
    """Corrections work by recording again, not editing."""
    app_id = _app(conn, 30)
    outcomes.record(conn, app_id, "screen", "2026-08-10")
    outcomes.record(conn, app_id, "rejected", "2026-08-20")
    assert outcomes.effective_outcome(conn, app_id) == "rejected"


def test_a_manual_outcome_beats_a_derived_no_response(conn):
    """The spec's day-40 case: a reply arrives after we assumed silence.
    The derived row is not deleted; it simply loses."""
    app_id = _app(conn, 40)
    outcomes.derive_no_response(conn, after_days=30)
    assert outcomes.effective_outcome(conn, app_id) == "no_response"

    outcomes.record(conn, app_id, "screen", "2026-08-21")

    assert outcomes.effective_outcome(conn, app_id) == "screen"
    assert conn.execute("SELECT COUNT(*) n FROM outcome").fetchone()["n"] == 2, \
        "history intact"


def test_callback_data_can_now_exist(conn):
    """The whole point of this change. The v1 -> v2 gate requires callback
    data, and today the denominator cannot leave zero because nothing
    produces a submitted application. Fails against the old code."""
    from career_agent import store
    job_id = conn.execute(
        "SELECT id FROM job LIMIT 1").fetchone()["id"]
    app_id = store.mark_applied(conn, job_id, "2026-08-20")
    outcomes.record(conn, app_id, "screen", "2026-08-21")
    assert outcomes.callback_rate(conn) == (1, 1)
