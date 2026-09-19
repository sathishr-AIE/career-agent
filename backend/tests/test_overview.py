import datetime as dt

import pytest

from career_agent import db, store
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


def test_outcome_summary_counts_this_month(conn):
    j = _job(conn, "this-month")
    app_id = conn.execute(
        "INSERT INTO application (job_id, resume_version, status,"
        " submitted_at) VALUES (?, 'v1', 'submitted',"
        " datetime('now', 'start of month', '+1 day'))", (j,)).lastrowid
    conn.execute("INSERT INTO outcome (application_id, type)"
                 " VALUES (?, 'interview')", (app_id,))
    conn.commit()
    summary = overview.outcome_summary(conn)
    assert summary["applied"] == 1
    assert summary["responses"] == 1
    assert summary["interviews"] == 1
    assert summary["offers"] == 0
    assert summary["callback_rate"] == 100.0
    assert summary["interview_rate"] == 100.0


def test_outcome_summary_uses_effective_outcome_not_raw_rows(conn):
    """A derived no_response should not count as a response once a real
    outcome supersedes it — outcome_summary must go through
    effective_outcome(), not just check whether any callback-type row
    exists."""
    j = _job(conn, "superseded")
    app_id = conn.execute(
        "INSERT INTO application (job_id, resume_version, status,"
        " submitted_at) VALUES (?, 'v1', 'submitted', datetime('now'))",
        (j,)).lastrowid
    conn.execute("INSERT INTO outcome (application_id, type, derived,"
                 " occurred_at) VALUES (?, 'screen', 0, datetime('now', '-1 day'))",
                 (app_id,))
    conn.execute("INSERT INTO outcome (application_id, type, derived,"
                 " occurred_at) VALUES (?, 'rejected', 0, datetime('now'))",
                 (app_id,))
    conn.commit()
    summary = overview.outcome_summary(conn)
    assert summary["responses"] == 0  # latest outcome is 'rejected', not a callback


def test_outcome_summary_zero_applications_avoids_division_by_zero(conn):
    summary = overview.outcome_summary(conn)
    assert summary == {"applied": 0, "responses": 0, "interviews": 0,
                        "offers": 0, "callback_rate": 0.0,
                        "interview_rate": 0.0}


def test_score_distribution_buckets(conn):
    j1 = _job(conn, "high")
    _scored(conn, j1, score=90)
    j2 = _job(conn, "good")
    _scored(conn, j2, score=65)
    j3 = _job(conn, "fair")
    _scored(conn, j3, score=45)
    j4 = _job(conn, "low")
    _scored(conn, j4, score=10)
    conn.commit()
    dist = overview.score_distribution(conn)
    assert dist["total"] == 4
    assert dist["high"] == 25.0
    assert dist["good"] == 25.0
    assert dist["fair"] == 25.0
    assert dist["low"] == 25.0


def test_score_distribution_does_not_double_count_a_rescored_job(conn):
    j = _job(conn, "rescored")
    _scored(conn, j, score=10)  # older pass
    _scored(conn, j, score=90)  # rescored, newer — this is the one that counts
    conn.commit()
    dist = overview.score_distribution(conn)
    assert dist["total"] == 1
    assert dist["high"] == 100.0
    assert dist["low"] == 0


def test_score_distribution_excludes_hard_skips(conn):
    j = _job(conn, "hard-skip")
    _hard_skip(conn, j)
    conn.commit()
    assert overview.score_distribution(conn)["total"] == 0


def test_source_performance_groups_by_source(conn):
    j1 = _job(conn, "li-1", source="linkedin")
    _scored(conn, j1, verdict="submit")
    j2 = _job(conn, "nk-1", source="naukri")
    _hard_skip(conn, j2)
    conn.commit()
    rows = {r["source"]: r for r in overview.source_performance(conn)}
    assert rows["linkedin"]["discovered"] == 1
    assert rows["linkedin"]["pass_rate"] == 100.0
    assert rows["naukri"]["pass_rate"] == 0.0


def test_source_performance_does_not_shortlist_a_demoted_job(conn):
    """COUNT(DISTINCT j.id) alone only prevents counting a rescored job
    twice — it doesn't stop a stale older row from mis-attributing it to
    the wrong bucket. A job demoted submit->skip must not still count as
    shortlisted via its old row."""
    j = _job(conn, "demoted", source="linkedin")
    _scored(conn, j, score=90, verdict="submit")  # older pass
    _scored(conn, j, score=20, verdict="skip")  # rescored, newer: demoted
    conn.commit()
    rows = {r["source"]: r for r in overview.source_performance(conn)}
    assert rows["linkedin"]["discovered"] == 1
    assert rows["linkedin"]["shortlist_rate"] == 0.0


def test_source_performance_response_rate(conn):
    j = _job(conn, "li-2", source="linkedin")
    _scored(conn, j, verdict="submit")
    app_id = conn.execute(
        "INSERT INTO application (job_id, resume_version, status,"
        " submitted_at) VALUES (?, 'v1', 'submitted', datetime('now'))",
        (j,)).lastrowid
    conn.execute("INSERT INTO outcome (application_id, type)"
                 " VALUES (?, 'screen')", (app_id,))
    conn.commit()
    rows = {r["source"]: r for r in overview.source_performance(conn)}
    assert rows["linkedin"]["applied"] == 1
    assert rows["linkedin"]["response_rate"] == 100.0


def test_recent_discoveries_gate_labels(conn):
    j1 = _job(conn, "passed")
    _scored(conn, j1, verdict="submit")
    j2 = _job(conn, "reviewed")
    _scored(conn, j2, verdict="skip")
    j3 = _job(conn, "failed")
    _hard_skip(conn, j3)
    conn.commit()
    gates = {d["title"] + d["source"] + str(d["id"]): d["gate"]
             for d in overview.recent_discoveries(conn)}
    by_id = {d["id"]: d["gate"] for d in overview.recent_discoveries(conn)}
    assert by_id[j1] == "pass"
    assert by_id[j2] == "review"
    assert by_id[j3] == "fail"


def test_recent_discoveries_does_not_duplicate_a_rescored_job(conn):
    j = _job(conn, "rescored")
    _scored(conn, j, score=40, verdict="skip")  # older pass
    _scored(conn, j, score=90, verdict="submit")  # rescored, newer
    conn.commit()
    rows = [d for d in overview.recent_discoveries(conn) if d["id"] == j]
    assert len(rows) == 1
    assert rows[0]["verdict"] == "submit"  # the latest assessment, not the old one


def test_recent_discoveries_respects_limit(conn):
    for i in range(15):
        _job(conn, f"j{i}")
    conn.commit()
    assert len(overview.recent_discoveries(conn, limit=5)) == 5


def test_recent_outcomes_labels_applied_when_no_outcome_yet(conn):
    j = _job(conn, "just-applied")
    conn.execute("INSERT INTO application (job_id, resume_version, status,"
                 " submitted_at) VALUES (?, 'v1', 'submitted', datetime('now'))",
                 (j,))
    conn.commit()
    outcomes_list = overview.recent_outcomes(conn)
    assert outcomes_list[0]["label"] == "Applied"


def test_recent_outcomes_orders_by_latest_outcome_not_submission(conn):
    """The panel exists to surface outcomes as they happen. An interview
    recorded today on a six-week-old application must outrank an application
    sent two days ago that nothing has happened to since."""
    old = _job(conn, "old-but-active")
    old_app = conn.execute(
        "INSERT INTO application (job_id, resume_version, status,"
        " submitted_at) VALUES (?, 'v1', 'submitted', datetime('now', '-42 days'))",
        (old,)).lastrowid
    conn.execute("INSERT INTO outcome (application_id, type, occurred_at)"
                 " VALUES (?, 'interview', datetime('now'))", (old_app,))
    fresh = _job(conn, "recent-but-quiet")
    conn.execute(
        "INSERT INTO application (job_id, resume_version, status,"
        " submitted_at) VALUES (?, 'v1', 'submitted', datetime('now', '-2 days'))",
        (fresh,))
    conn.commit()

    rows = overview.recent_outcomes(conn)
    assert [r["label"] for r in rows] == ["Interview", "Applied"]
    # and the timestamp the template renders is the activity, not the send
    assert rows[0]["activity_at"] > rows[1]["activity_at"]


def test_recent_outcomes_labels_interview(conn):
    j = _job(conn, "interviewing")
    app_id = conn.execute(
        "INSERT INTO application (job_id, resume_version, status,"
        " submitted_at) VALUES (?, 'v1', 'submitted', datetime('now'))",
        (j,)).lastrowid
    conn.execute("INSERT INTO outcome (application_id, type)"
                 " VALUES (?, 'interview')", (app_id,))
    conn.commit()
    outcomes_list = overview.recent_outcomes(conn)
    assert outcomes_list[0]["label"] == "Interview"


def test_recent_events_newest_first_with_job_and_no_pipeline_noise(conn):
    """EV1: the Dashboard's global activity feed."""
    jid = _job(conn, "fp1")
    store.log(conn, jid, "human_applied")
    store.log(conn, None, "pipeline_progress", "Scoring 3 of 10")
    store.log(conn, None, "run_started", "manual")
    rows = overview.recent_events(conn)
    assert [r["type"] for r in rows] == ["run_started", "human_applied"]
    assert rows[0]["company"] is None and rows[0]["payload"] == "manual"
    assert rows[1]["company"] == "Acme" and rows[1]["title"] == "AI Engineer"
    assert len(overview.recent_events(conn, limit=1)) == 1
    assert rows[0]["recent"] == 1
    conn.execute("UPDATE event SET occurred_at = datetime('now', '-2 days')"
                 " WHERE type = 'human_applied'")
    assert overview.recent_events(conn)[1]["recent"] == 0
