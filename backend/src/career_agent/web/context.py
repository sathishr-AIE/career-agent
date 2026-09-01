"""Page-data builders shared by the Jinja routes (app.py) and the JSON API
(api.py). Each function returns a plain dict of the data a page needs -- no
Request, no Template, nothing HTML-shaped -- so the two frontends read from
exactly one data path and can't drift apart during the migration described in
docs/ (or, until that doc lands, the split-frontend plan)."""
import datetime as dt
import json
import sqlite3
from pathlib import Path

from career_agent import outcomes, store, tailor
from career_agent.apply import ats as ats_apply
from career_agent.config import (MODEL_LABELS, SCORING_MODELS,
                                 CandidateProfile, load_brief,
                                 load_candidate_profile)
from career_agent.web import overview, worker

LIST_SQL = """
SELECT j.id, j.company, j.title, j.location, j.source, j.url,
       a.verdict, a.rationale, a.stage, a.weighted_score AS score,
       (SELECT ap.status FROM application ap
         WHERE ap.job_id = j.id
           AND ap.status IN ('in_flight','submitted','held_unknown','failed_permanent')
         ORDER BY ap.id DESC LIMIT 1) AS terminal_status,
       (SELECT ap.status FROM application ap WHERE ap.job_id = j.id
         ORDER BY ap.id DESC LIMIT 1) IS 'draft' AS has_draft,
       (SELECT ap.resume_version FROM application ap WHERE ap.job_id = j.id
         ORDER BY ap.id DESC LIMIT 1) AS resume_version
  FROM job j JOIN assessment a ON a.job_id = j.id
 WHERE j.merged_into_job_id IS NULL AND a.verdict IN ({placeholders})
 ORDER BY a.weighted_score DESC NULLS LAST, j.discovered_at DESC
"""

# order matters: the template (and the JSON stage track) renders anything
# before the current stage as done and the current one as active
STAGES = (("discover", "Discover"), ("clean", "Clean"), ("filter", "Filter"),
          ("score", "Score"), ("ready", "Ready"))


def utc_today() -> dt.date:
    # SQLite's date('now') -- what the daily-cap guard (worker.guard) and
    # every other date('now') comparison in this app use -- is UTC.
    # dt.date.today() is the host's local calendar day, which disagrees with
    # UTC for part of every day off-UTC (e.g. Chennai, UTC+5:30, roughly
    # 00:00-05:30 IST). A date stamped with local "today" during that window
    # never matches date('now') in the cap query, so the cap silently stops
    # counting. Same fix as overview.sparkline_values -- keep them matching.
    return dt.datetime.now(dt.timezone.utc).date()


def today_submitted(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        " AND date(submitted_at) = date('now')").fetchone()["n"]


def load_candidate_profile_or_none(path: Path) -> CandidateProfile | None:
    try:
        return load_candidate_profile(path)
    except FileNotFoundError:
        return None


def overview_context(conn: sqlite3.Connection, brief_path: Path) -> dict:
    brief = load_brief(brief_path)
    kpi_data = overview.kpis(conn)
    return {"brief": brief, "daily_cap": brief.daily_cap,
            "today_submitted": today_submitted(conn),
            "kpis": kpi_data,
            "outcome_summary": overview.outcome_summary(conn),
            "source_performance": overview.source_performance(conn),
            "score_distribution": overview.score_distribution(conn),
            "recent_discoveries": overview.recent_discoveries(conn),
            "recent_outcomes": overview.recent_outcomes(conn),
            "shortlisted_count": kpi_data["shortlisted"],
            **pipeline_status_context(conn)}


def resumes_context(conn: sqlite3.Connection, brief_path: Path) -> dict:
    brief = load_brief(brief_path)
    master_path = tailor.TEMPLATE_PATH
    master = {"exists": master_path.exists(), "path": str(master_path)}
    if master["exists"]:
        # Local time, to agree with r.created_at below -- SQLite's own
        # datetime('now') is UTC, and this page must not show two clocks
        # side by side (see overview.py's _utc_today for the general trap).
        master["modified"] = dt.datetime.fromtimestamp(
            master_path.stat().st_mtime).isoformat(timespec="seconds")

    claims = {r["id"]: r["claim"]
             for r in conn.execute("SELECT id, claim FROM fact")}

    versions = []
    for r in conn.execute(
            "SELECT r.version, datetime(r.created_at, 'localtime') AS created_at,"
            "       r.content, j.company, j.title"
            "  FROM resume r JOIN job j ON j.id = r.job_id"
            " ORDER BY r.id DESC"):
        content = json.loads(r["content"]) if r["content"] else {}
        bullets = content.get("bullets", [])
        for b in bullets:
            b["fact_claims"] = [claims.get(fid, "unknown fact")
                               for fid in b.get("fact_ids", [])]
        versions.append({**dict(r), "summary": content.get("summary", ""),
                         "bullets": bullets})

    return {"brief": brief, "daily_cap": brief.daily_cap,
            "today_submitted": today_submitted(conn),
            "master": master, "versions": versions}


def applications_context(conn: sqlite3.Connection, show: str,
                         brief_path: Path, *, scheduled: bool) -> dict:
    verdicts = ["skip"] if show == "skipped" else ["submit", "hold"]
    sql = LIST_SQL.format(placeholders=",".join("?" * len(verdicts)))
    rows = conn.execute(sql, verdicts).fetchall()
    all_rows = conn.execute(
        LIST_SQL.format(placeholders="?,?,?"), ["submit", "hold", "skip"]
    ).fetchall()
    brief = load_brief(brief_path)

    # application id + current effective outcome per job, for the outcome
    # controls. One effective_outcome call per submitted row, matching what
    # overview.recent_outcomes already does and bounded by the page size.
    applied = {}
    for r in conn.execute(
            "SELECT id, job_id FROM application WHERE status = 'submitted'"):
        applied[r["job_id"]] = {
            "application_id": r["id"],
            "outcome": outcomes.effective_outcome(conn, r["id"]),
        }

    tailored_sent = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        " AND resume_version LIKE 'tailored-%'").fetchone()["n"]
    outcomes_recorded = conn.execute("SELECT COUNT(*) n FROM outcome").fetchone()["n"]

    return {"jobs": rows if show != "skipped" else all_rows,
            "skipped_jobs": [r for r in all_rows if r["verdict"] == "skip"],
            "show": show, "scheduled": scheduled,
            "brief": brief, "daily_cap": brief.daily_cap,
            "today_submitted": today_submitted(conn),
            "applied": applied,
            "manual_types": outcomes.MANUAL_TYPES,
            "outcome_labels": overview.CALLBACK_LABELS,
            "today": utc_today().isoformat(),
            "submission_implemented": ats_apply.SUBMISSION_IMPLEMENTED,
            "tailored_sent": tailored_sent,
            "outcomes_recorded": outcomes_recorded,
            **run_status_context(conn)}


def settings_context(conn: sqlite3.Connection, brief_path: Path,
                     candidate_path: Path, *, form=None, errors=None,
                     saved=False) -> dict:
    """Values shown come from the stores unless a failed submission is being
    re-rendered, in which case the user's own input is preserved."""
    brief = None
    brief_error = None
    try:
        brief = load_brief(brief_path)
    except Exception as exc:
        # Falling back to CareerBrief() defaults would be a trap: saving
        # them would overwrite the file the user lost with a brief they
        # never chose. Disable that half of the form instead.
        brief_error = f"{brief_path} could not be read: {exc}"

    candidate = None
    candidate_error = None
    try:
        candidate = load_candidate_profile(candidate_path)
    except FileNotFoundError:
        pass  # first run: show blank fields, not an error
    except Exception as exc:
        candidate_error = f"{candidate_path} could not be read: {exc}"

    settings = store.get_settings(conn)
    return {"brief": brief, "brief_error": brief_error,
            "candidate": candidate, "candidate_error": candidate_error,
            "daily_cap": brief.daily_cap if brief else "-",
            "today_submitted": today_submitted(conn),
            "settings": settings, "scoring_models": SCORING_MODELS,
            "model_labels": MODEL_LABELS, "form": form or {},
            "errors": errors or {}, "saved": saved}


def run_status_context(conn: sqlite3.Connection) -> dict:
    state = worker.get_run_state(conn, "apply")
    current_job = None
    needs_answer_question = None
    draft_answers = None
    if state["current_job_id"]:
        current_job = conn.execute(
            "SELECT j.id AS job_id, j.company, j.title FROM job j"
            " WHERE j.id = ?", (state["current_job_id"],)).fetchone()
        draft = conn.execute(
            "SELECT answers FROM application WHERE job_id = ? AND status = 'draft'"
            " ORDER BY id DESC LIMIT 1", (state["current_job_id"],)).fetchone()
        if draft is not None:
            if draft["answers"] is not None:
                draft_answers = json.loads(draft["answers"])
        else:
            # Only show the card if the most recent needs_answer event for
            # this job is newer than the most recent needs_answer_resolved
            # event for it (or nothing has resolved it yet). Without this,
            # answering re-parks a stale "Answer needed" card during the
            # window between the worker re-picking the job (setting
            # current_job_id again) and its draft actually landing.
            row = conn.execute(
                "SELECT payload FROM event WHERE job_id = ? AND type = 'needs_answer'"
                " AND id > COALESCE((SELECT MAX(id) FROM event"
                "                     WHERE job_id = ? AND type = 'needs_answer_resolved'), 0)"
                " ORDER BY id DESC LIMIT 1",
                (state["current_job_id"], state["current_job_id"])).fetchone()
            needs_answer_question = row["payload"] if row else None
    submitted = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
    ).fetchone()["n"]
    failed = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'failed_permanent'"
    ).fetchone()["n"]
    # jobs the gate skipped and nobody overrode (an override would have left
    # an application row behind), counted once via the latest assessment
    gate_skipped = conn.execute(
        "SELECT COUNT(*) n FROM job j JOIN assessment a ON a.job_id = j.id"
        " WHERE j.merged_into_job_id IS NULL AND a.verdict = 'skip'"
        "   AND a.id = (SELECT id FROM assessment a2 WHERE a2.job_id = j.id"
        "               ORDER BY a2.created_at DESC, a2.id DESC LIMIT 1)"
        "   AND NOT EXISTS (SELECT 1 FROM event e WHERE e.job_id = j.id"
        "                     AND e.type = 'human_override')"
        "   AND NOT EXISTS (SELECT 1 FROM application ap WHERE ap.job_id = j.id)"
    ).fetchone()["n"]
    stats = {
        "total_applied": submitted,
        "queued": worker.queue_count(conn),
        "in_progress": 1 if state["current_job_id"] else 0,
        "successful": submitted,
        "failed_skipped": failed + gate_skipped,
    }
    recent_events = conn.execute(
        "SELECT type, payload, occurred_at FROM event"
        # the pipeline's own feed lives on the Overview page; an apply
        # activity log that shows discovery progress is showing the wrong
        # thing, and at ~10 rows a run it would show nothing else
        " WHERE type NOT LIKE 'pipeline_%'"
        " ORDER BY id DESC LIMIT 10").fetchall()
    return {"run_state": state, "current_job": current_job, "stats": stats,
            "recent_events": recent_events,
            "needs_answer_question": needs_answer_question,
            "draft_answers": draft_answers,
            # Read once, cheaply, so the always-visible status bar can show
            # the kill switch's state without a second endpoint just for it.
            "submission_implemented": ats_apply.SUBMISSION_IMPLEMENTED}


def pipeline_status_context(conn: sqlite3.Connection) -> dict:
    """Shared by the polling route/endpoint and the Overview page's first
    paint (the include in overview.html renders before the poller's own
    hx-trigger="load" ever fires), so both always agree."""
    state = worker.get_run_state(conn, "pipeline")
    # Scoped to this run: without it, a freshly started run shows the
    # *previous* run's dozen lines next to all-zero counters. started_at is
    # NULL before any run has ever started, which correctly yields no rows
    # (NULL comparisons are never true in SQLite) rather than everything.
    feed = conn.execute(
        "SELECT payload, occurred_at FROM event"
        " WHERE type = 'pipeline_progress'"
        "   AND occurred_at >= (SELECT started_at FROM run_state"
        "                        WHERE kind = 'pipeline')"
        " ORDER BY id DESC LIMIT 12").fetchall()
    settings = store.get_settings(conn)
    return {"pipeline_state": state, "feed": feed, "stages": STAGES,
            "max_score": settings["max_score_per_run"]}
