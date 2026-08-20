import sqlite3

from career_agent import normalize
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
