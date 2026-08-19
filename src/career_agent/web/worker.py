import sqlite3
from pathlib import Path

from career_agent.config import load_brief

CANDIDATE_SQL = """
SELECT j.id AS job_id, j.company, j.title, a.weighted_score
  FROM job j
  JOIN assessment a ON a.job_id = j.id
 WHERE j.merged_into_job_id IS NULL
   AND a.verdict IN ('submit','hold')
   AND a.id = (SELECT id FROM assessment a2 WHERE a2.job_id = j.id
               ORDER BY a2.created_at DESC, a2.id DESC LIMIT 1)
   AND NOT EXISTS (
       SELECT 1 FROM application ap
        WHERE ap.job_id = j.id
          AND ap.status IN ('in_flight','submitted','held_unknown',
                             'failed_permanent')
   )
 ORDER BY j.priority ASC NULLS LAST, a.weighted_score DESC
 LIMIT 1
"""


def get_run_state(conn: sqlite3.Connection, kind: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM run_state WHERE kind = ?", (kind,)).fetchone()


def set_run_state(conn: sqlite3.Connection, kind: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE run_state SET {cols}, updated_at = datetime('now')"
        " WHERE kind = ?", (*fields.values(), kind))
    conn.commit()


def next_candidate(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(CANDIDATE_SQL).fetchone()


def guard(conn: sqlite3.Connection, job_id: int, allow_skip: bool,
          brief_path: Path) -> str | None:
    """Dashboard-side guardrail. The partial unique index is the real
    guarantee; this exists to produce a readable message."""
    a = conn.execute("SELECT verdict FROM assessment WHERE job_id = ?"
                     " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    if a is None:
        return "This job has not been scored yet."
    if a["verdict"] == "skip" and not allow_skip:
        return "The gate skipped this one. Use Apply anyway to override."

    brief = load_brief(brief_path)
    used = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        " AND date(submitted_at) = date('now')").fetchone()["n"]
    if used >= brief.daily_cap:
        return f"Daily cap of {brief.daily_cap} reached."

    paused = conn.execute(
        "SELECT payload FROM event WHERE type='pause'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    if paused and paused["payload"] == "on":
        return "The agent is paused."
    return None


def queue_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) n FROM job j JOIN assessment a ON a.job_id = j.id"
        " WHERE j.merged_into_job_id IS NULL"
        "   AND a.verdict IN ('submit','hold')"
        "   AND a.id = (SELECT id FROM assessment a2 WHERE a2.job_id = j.id"
        "               ORDER BY a2.created_at DESC, a2.id DESC LIMIT 1)"
        "   AND NOT EXISTS (SELECT 1 FROM application ap"
        "                    WHERE ap.job_id = j.id"
        "                      AND ap.status IN ('in_flight','submitted',"
        "                                        'held_unknown','failed_permanent'))"
    ).fetchone()["n"]
