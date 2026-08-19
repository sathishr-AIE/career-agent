import datetime as dt

import pytest

from career_agent import db
from career_agent.web import overview


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    return c


def _job(conn, fp, source="linkedin", days_ago=0):
    return conn.execute(
        "INSERT INTO job (fingerprint, source, external_id, company,"
        " company_normalized, title, title_normalized, discovered_at)"
        " VALUES (?, ?, ?, 'Acme', 'acme', 'AI Engineer', 'aiengineer',"
        " datetime('now', ?))",
        (fp, source, fp, f"-{days_ago} days")).lastrowid


def _scored(conn, job_id, score=80, verdict="submit"):
    conn.execute(
        "INSERT INTO assessment (job_id, stage, weighted_score, verdict,"
        " rationale, model, prompt_version)"
        " VALUES (?, 'scored', ?, ?, 'r', 'm', 'v1')",
        (job_id, score, verdict))


def _hard_skip(conn, job_id):
    conn.execute(
        "INSERT INTO assessment (job_id, stage, verdict, rationale, model,"
        " prompt_version) VALUES (?, 'hard', 'skip', 'reason', 'hardfilter', 'n/a')",
        (job_id,))


def test_kpis_discovered_counts_only_today(conn):
    _job(conn, "today", days_ago=0)
    _job(conn, "yesterday", days_ago=1)
    conn.commit()
    assert overview.kpis(conn)["discovered"] == 1


def test_kpis_after_hard_filter_excludes_hard_skips(conn):
    j1 = _job(conn, "survives")
    _scored(conn, j1)
    j2 = _job(conn, "fails-hard")
    _hard_skip(conn, j2)
    conn.commit()
    assert overview.kpis(conn)["after_hard_filter"] == 1


def test_kpis_shortlisted_excludes_scored_skips(conn):
    j1 = _job(conn, "submit-me")
    _scored(conn, j1, verdict="submit")
    j2 = _job(conn, "skip-me")
    _scored(conn, j2, verdict="skip")
    conn.commit()
    assert overview.kpis(conn)["shortlisted"] == 1


def test_kpis_applied_counts_submitted_today(conn):
    j = _job(conn, "applied-today")
    conn.execute("INSERT INTO application (job_id, resume_version, status,"
                 " submitted_at) VALUES (?, 'v1', 'submitted', datetime('now'))",
                 (j,))
    conn.commit()
    assert overview.kpis(conn)["applied"] == 1


def test_kpis_responses_matches_callback_rate_numerator(conn):
    j = _job(conn, "with-response")
    app_id = conn.execute(
        "INSERT INTO application (job_id, resume_version, status,"
        " submitted_at) VALUES (?, 'v1', 'submitted', datetime('now'))",
        (j,)).lastrowid
    conn.execute("INSERT INTO outcome (application_id, type) VALUES (?, 'screen')",
                 (app_id,))
    conn.commit()
    assert overview.kpis(conn)["responses"] == 1


def test_kpis_does_not_double_count_a_rescored_job(conn):
    """A job can pick up a second 'scored' assessment row when the gate's
    prompt_version bumps (store.unscored_jobs re-surfaces it; save_assessment
    always inserts, never upserts). after_hard_filter/shortlisted must count
    the job once, using its latest assessment, not once per row."""
    j = _job(conn, "rescored")
    _scored(conn, j, score=40, verdict="skip")  # first pass, older
    _scored(conn, j, score=90, verdict="submit")  # rescored, newer
    conn.commit()
    kpi_data = overview.kpis(conn)
    assert kpi_data["after_hard_filter"] == 1
    assert kpi_data["shortlisted"] == 1  # only the latest verdict counts


def test_kpis_sparklines_have_seven_points_per_metric(conn):
    _job(conn, "a")
    conn.commit()
    sparks = overview.kpis(conn)["sparklines"]
    assert set(sparks) == {"discovered", "after_hard_filter", "shortlisted",
                           "applied", "responses"}
    for points in sparks.values():
        assert len(points.split()) == 7


def test_sparkline_values_anchors_to_utc_today_not_host_local_clock(conn, monkeypatch):
    """sparkline_values must agree with the SQL it's paired with, which
    always buckets by SQLite's date('now') -- UTC. Simulates the window
    (e.g. ~05:30-11:00 IST in Chennai, UTC+5:30) where a host's local
    calendar day is still behind UTC's, by making the two clocks disagree
    on purpose: if the function read the local clock, this would bucket
    under 2026-01-01 and the assertion would fail."""
    class _FixedLocalDate(dt.date):
        @classmethod
        def today(cls):
            return dt.date(2026, 1, 1)

    class _FixedUTCDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 1, 2, 1, 0, tzinfo=tz)

    monkeypatch.setattr(overview.dt, "date", _FixedLocalDate)
    monkeypatch.setattr(overview.dt, "datetime", _FixedUTCDatetime)

    values = overview.sparkline_values(conn, "SELECT '2026-01-02' day, 5 n")

    assert values[-1] == 5  # bucketed under UTC's 2026-01-02, not local's 01-01


def test_sparkline_points_flat_line_when_all_zero():
    # Deviation from the brief: the brief's snippet collapsed an all-zero
    # series to the 2 endpoints ("0,24 63,24"), but that directly conflicts
    # with test_kpis_sparklines_have_seven_points_per_metric below, which
    # requires every sparkline -- including all-zero ones, the normal case
    # for a quiet metric over 7 days -- to keep one point per day. A flat
    # line still renders identically whether it has 2 or N collinear
    # points, so the fix keeps the point count and drops the collapse.
    assert overview.sparkline_points([0, 0, 0]) == "0.0,24.0 31.5,24.0 63.0,24.0"


def test_sparkline_points_scales_to_a_64_by_28_box():
    points = overview.sparkline_points([0, 5, 10])
    xs = [float(p.split(",")[0]) for p in points.split()]
    ys = [float(p.split(",")[1]) for p in points.split()]
    assert xs[0] == 0.0 and xs[-1] == 63.0
    assert min(ys) == 4.0 and max(ys) == 24.0
