import asyncio
import sqlite3
from pathlib import Path

from career_agent import store
from career_agent.apply import ats as ats_apply
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


async def apply_tick(conn: sqlite3.Connection, brief_path) -> None:
    """One step of the apply worker: pick a candidate, gate it, draft it,
    and in auto mode send it. Called by the control endpoints (for
    immediate feedback) and by the background loop (to keep going
    unattended). A no-op unless the apply run is 'running' and not
    already blocked on a manual-mode draft awaiting review."""
    state = get_run_state(conn, "apply")
    if state["status"] != "running":
        return
    if state["current_job_id"] is not None:
        return

    candidate = next_candidate(conn)
    if candidate is None:
        set_run_state(conn, "apply", status="idle", current_job_id=None)
        store.log(conn, None, "run_completed")
        return

    job_id = candidate["job_id"]
    set_run_state(conn, "apply", current_job_id=job_id)

    denial = guard(conn, job_id, allow_skip=True, brief_path=brief_path)
    if denial:
        if "cap" in denial.lower():
            set_run_state(conn, "apply", status="paused", current_job_id=None)
            store.log(conn, job_id, "run_autopaused", denial)
        else:
            store.log(conn, job_id, "job_skipped", denial)
            set_run_state(conn, "apply", current_job_id=None)
        return

    try:
        result = await ats_apply.submit(conn, job_id, dry_run=True)
    except Exception as exc:
        set_run_state(conn, "apply", status="error", current_job_id=None,
                      last_error=str(exc))
        store.log(conn, job_id, "run_error", str(exc))
        return

    if not result["ok"]:
        store.log(conn, job_id, "job_skipped", result.get("reason", ""))
        set_run_state(conn, "apply", current_job_id=None)
        return

    if state["mode"] == "manual":
        return  # stays 'running' with current_job_id set: awaiting review

    try:
        await ats_apply.submit(conn, job_id, dry_run=False)
    except Exception as exc:
        set_run_state(conn, "apply", status="error", current_job_id=None,
                      last_error=str(exc))
        store.log(conn, job_id, "run_error", str(exc))
        return
    set_run_state(conn, "apply", current_job_id=None)


async def apply_worker_loop(conn_factory, brief_path) -> None:
    """Keeps the apply run advancing without anyone polling — the piece
    that makes Start actually mean 'walk away'. conn_factory is a
    zero-arg callable (web/app.py's _conn) so each iteration gets a
    fresh connection, matching the rest of the app's per-call pattern."""
    while True:
        conn = conn_factory()
        state = get_run_state(conn, "apply")
        if state["status"] == "running" and state["current_job_id"] is None:
            try:
                await apply_tick(conn, brief_path)
            except Exception as exc:
                set_run_state(conn, "apply", status="error", current_job_id=None,
                              last_error=str(exc))
                store.log(conn, None, "run_error", str(exc))
        else:
            await asyncio.sleep(1)


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
