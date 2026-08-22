import sqlite3

from career_agent import normalize
from career_agent.apply import ats
from career_agent.config import SCORING_MODELS, CareerBrief
from career_agent.models import Job, Verdict


def log(conn, job_id: int | None, type_: str, payload: str | None = None) -> None:
    conn.execute("INSERT INTO event (job_id, type, payload) VALUES (?, ?, ?)",
                 (job_id, type_, payload))
    conn.commit()


def upsert_jobs(conn, jobs: list[Job], brief: CareerBrief) -> int:
    """Insert jobs that are new and fresh. Returns how many were inserted."""
    inserted = 0
    for job in jobs:
        if normalize.is_stale(job, brief.staleness_days):
            continue
        try:
            conn.execute(
                "INSERT INTO job (fingerprint, source, external_id, company,"
                " company_normalized, title, title_normalized, location,"
                " is_remote, comp_min, comp_max, posted_at, url, description)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (normalize.fingerprint(job), job.source, job.external_id,
                 job.company, normalize.company(job.company), job.title,
                 normalize.title(job.title), job.location, int(job.is_remote),
                 job.comp_min, job.comp_max, job.posted_at, job.url,
                 job.description))
            inserted += 1
        except sqlite3.IntegrityError:
            continue  # same role, already seen from another board
    conn.commit()
    return inserted


def save_hard_skip(conn, job_id: int, reason: str) -> None:
    conn.execute(
        "INSERT INTO assessment (job_id, stage, verdict, rationale, model,"
        " prompt_version) VALUES (?, 'hard', 'skip', ?, 'hardfilter', 'n/a')",
        (job_id, reason))
    conn.commit()


def save_assessment(conn, job_id: int, v: Verdict, model: str,
                    prompt_version: str) -> None:
    conn.execute(
        "INSERT INTO assessment (job_id, stage, role_fit, credibility,"
        " opportunity, application_quality, eligibility_soft, weighted_score,"
        " verdict, rationale, model, prompt_version)"
        " VALUES (?, 'scored', ?,?,?,?,?,?,?,?,?,?)",
        (job_id, v.role_fit, v.credibility, v.opportunity, v.application_quality,
         v.eligibility_soft, v.weighted, v.verdict, v.rationale, model,
         prompt_version))
    conn.commit()


def unscored_jobs(conn, prompt_version: str,
                  limit: int | None = None) -> list[sqlite3.Row]:
    """Jobs with no assessment at the current prompt version, excluding
    hard-filter skips and merged duplicates. Bumping the version brings
    previously scored jobs back, which is what makes prompt changes measurable.

    limit=None returns the whole pool, which is what callers want: rationing
    rows here would let hard-filtered jobs consume a scoring budget they
    never spend a model call against."""
    sql = ("SELECT j.* FROM job j"
           " WHERE j.merged_into_job_id IS NULL"
           "   AND NOT EXISTS (SELECT 1 FROM assessment a WHERE a.job_id = j.id"
           "                     AND (a.stage = 'hard'"
           "                          OR a.prompt_version = ?))"
           " ORDER BY j.discovered_at DESC")
    params: list = [prompt_version]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def facts(conn) -> list[str]:
    rows = conn.execute("SELECT claim, evidence FROM fact ORDER BY id").fetchall()
    return [f"{r['claim']} (evidence: {r['evidence']})" for r in rows]


def fact_rows(conn) -> list[tuple[int, str]]:
    """Same text format as facts(), paired with each row's id so tailor.py
    can ask the model to cite which facts a bullet draws from, and validate
    that citation against real rows before anything is rendered."""
    rows = conn.execute("SELECT id, claim, evidence FROM fact ORDER BY id").fetchall()
    return [(r["id"], f"{r['claim']} (evidence: {r['evidence']})") for r in rows]


def latest_resume_version(conn, job_id: int) -> str | None:
    row = conn.execute(
        "SELECT version FROM resume WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (job_id,)).fetchone()
    return row["version"] if row else None


def resume_version_for(conn, job_id: int) -> str:
    """What to record on an application for this job: the most recent
    tailored version if one exists, else the untailored fallback constant."""
    return latest_resume_version(conn, job_id) or ats.RESUME_VERSION


def next_resume_version(conn, job_id: int) -> str:
    n = conn.execute("SELECT COUNT(*) n FROM resume WHERE job_id = ?",
                     (job_id,)).fetchone()["n"]
    return f"tailored-{job_id}-r{n + 1}"


def insert_resume(conn, job_id: int, version: str, path: str,
                  content: str) -> str:
    """Insert a new resume row. resume.version is UNIQUE, and two
    near-simultaneous Apply clicks on the same job can both compute the
    same next_resume_version() before either commits -- handled the same
    way upsert_jobs handles the equivalent race on job.fingerprint: attempt
    the insert, and on a collision report back whichever row actually won
    rather than raising."""
    try:
        conn.execute(
            "INSERT INTO resume (version, path, job_id, content)"
            " VALUES (?, ?, ?, ?)", (version, path, job_id, content))
        conn.commit()
        return version
    except sqlite3.IntegrityError:
        return latest_resume_version(conn, job_id)


def get_settings(conn) -> sqlite3.Row:
    return conn.execute("SELECT * FROM setting WHERE id = 1").fetchone()


def save_settings(conn, scoring_model: str, max_score_per_run: int) -> None:
    if scoring_model not in SCORING_MODELS:
        raise ValueError(f"unknown scoring model: {scoring_model}")
    if max_score_per_run < 0:
        raise ValueError("max_score_per_run cannot be negative")
    conn.execute(
        "UPDATE setting SET scoring_model = ?, max_score_per_run = ?,"
        " updated_at = datetime('now') WHERE id = 1",
        (scoring_model, max_score_per_run))
    conn.commit()


def mark_applied(conn, job_id: int, when: str) -> int:
    """Record that a human applied to this job on the site themselves.

    This is the callback-rate denominator. Nothing else produces it: the
    agent does not submit (v3, and Naukri never), so without this the
    denominator stays zero and no outcome can be attached to anything.

    Promotes an existing draft when there is one so a single application
    attempt stays a single row. Raises sqlite3.IntegrityError via the
    one_live_application_per_job index if the job already has a live
    application.
    """
    draft = conn.execute(
        "SELECT id FROM application WHERE job_id = ? AND status = 'draft'"
        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()

    if draft is None:
        # answers stays NULL: for a manual application we do not know what
        # was sent, and saying so is better than copying a placeholder.
        cur = conn.execute(
            "INSERT INTO application (job_id, resume_version, status,"
            " submitted_at) VALUES (?, ?, 'submitted', ?)",
            (job_id, resume_version_for(conn, job_id), when))
        app_id = cur.lastrowid
    else:
        app_id = draft["id"]
        # answers is nulled rather than kept: the draft's answers are
        # precisely what was NOT sent, so carrying the stub filler's
        # placeholder forward would make the row read as a record of what the
        # human submitted -- which a future real Send is meant to rely on.
        conn.execute(
            "UPDATE application SET status = 'submitted', submitted_at = ?,"
            " answers = NULL WHERE id = ?", (when, app_id))

    conn.commit()
    log(conn, job_id, "human_marked_applied", when)
    return app_id
