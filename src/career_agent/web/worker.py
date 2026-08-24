import asyncio
import functools
import sqlite3
from pathlib import Path

from career_agent import run as run_module
from career_agent import store, tailor
from career_agent.apply import ats as ats_apply
from career_agent.config import load_brief, load_candidate_profile

# The one definition of "is this job in the apply queue". Anything that
# counts, picks, or reorders the queue joins job j + assessment a and uses
# this predicate, so the three can't drift apart.
QUEUE_WHERE = """
   j.merged_into_job_id IS NULL
   AND a.verdict IN ('submit','hold')
   AND a.id = (SELECT id FROM assessment a2 WHERE a2.job_id = j.id
               ORDER BY a2.created_at DESC, a2.id DESC LIMIT 1)
   AND NOT EXISTS (
       SELECT 1 FROM application ap
        WHERE ap.job_id = j.id
          AND ap.status IN ('in_flight','submitted','held_unknown',
                             'failed_permanent')
   )
   AND (SELECT ap.status FROM application ap WHERE ap.job_id = j.id
        ORDER BY ap.id DESC LIMIT 1) IS NOT 'draft'
"""

CANDIDATE_SQL = f"""
SELECT j.id AS job_id, j.company, j.title, a.weighted_score
  FROM job j
  JOIN assessment a ON a.job_id = j.id
 WHERE {QUEUE_WHERE}
 ORDER BY j.priority ASC NULLS LAST, a.weighted_score DESC
 LIMIT 1
"""


def get_run_state(conn: sqlite3.Connection, kind: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM run_state WHERE kind = ?", (kind,)).fetchone()


def set_run_state(conn: sqlite3.Connection, kind: str, **fields) -> None:
    if not fields:
        return  # nothing to set; an empty SET clause is invalid SQL
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


async def tailor_for_apply(conn: sqlite3.Connection, job_id: int,
                           brief_path: Path) -> str:
    """Tailor and render this job's resume if one doesn't exist yet, else
    reuse the most recent version. Shared by the dashboard's Apply button
    (app.py's _do_apply) and this module's apply_tick -- both create a
    draft the same way, so both need the same resume behind it."""
    run_module.verify_auth()
    row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
    job = run_module._row_to_job(row)
    brief = load_brief(brief_path)
    settings = store.get_settings(conn)
    ask = functools.partial(run_module._ask, model=settings["scoring_model"])
    return await tailor.ensure_tailored(conn, job_id, job, brief, ask)


async def apply_tick(conn: sqlite3.Connection, brief_path, profile_path) -> None:
    """One step of the apply worker: pick a candidate, gate it, draft it,
    and in auto mode send it. Called by the control endpoints (for
    immediate feedback) and by the background loop (to keep going
    unattended). A no-op unless the apply run is 'running' and not
    already blocked on a manual-mode draft, or a needs-answer park,
    awaiting review."""
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
        resume_version = await tailor_for_apply(conn, job_id, brief_path)
    except Exception as exc:
        set_run_state(conn, "apply", status="error", current_job_id=None,
                      last_error=str(exc))
        store.log(conn, job_id, "run_error", str(exc))
        return

    brief = load_brief(brief_path)
    try:
        profile = load_candidate_profile(profile_path)
    except FileNotFoundError:
        # Only a Greenhouse draft/send actually needs this -- see
        # ats._default_compute_answers, which raises a clear error itself
        # if it's reached with profile=None. Every other source drafts
        # fine without it, same as before this file needed a profile.
        profile = None

    try:
        result = await ats_apply.submit(conn, job_id, dry_run=True,
                                        brief=brief, profile=profile,
                                        resume_version=resume_version)
    except Exception as exc:
        set_run_state(conn, "apply", status="error", current_job_id=None,
                      last_error=str(exc))
        store.log(conn, job_id, "run_error", str(exc))
        return

    if result.get("needs_answer"):
        # Leave current_job_id set: apply_worker_loop's outer check
        # (current_job_id is None) keeps this exact job from being
        # re-picked on the next tick, so a question with no answer is a
        # stop, not a spin. The dashboard's "Answer needed" card
        # (app.py/_run_status.html) is what clears this park.
        store.log(conn, job_id, "needs_answer", result["needs_answer"])
        return

    if not result["ok"]:
        store.log(conn, job_id, "job_skipped", result.get("reason", ""))
        set_run_state(conn, "apply", current_job_id=None)
        return

    if state["mode"] == "manual":
        return  # stays 'running' with current_job_id set: awaiting review

    try:
        result = await ats_apply.submit(conn, job_id, dry_run=False,
                                        brief=brief, profile=profile,
                                        resume_version=store.resume_version_for(
                                            conn, job_id))
    except Exception as exc:
        set_run_state(conn, "apply", status="error", current_job_id=None,
                      last_error=str(exc))
        store.log(conn, job_id, "run_error", str(exc))
        return
    if not result["ok"]:
        # captcha hold / permanent failure: reported, not raised
        store.log(conn, job_id, "job_skipped", result.get("reason", ""))
    set_run_state(conn, "apply", current_job_id=None)


async def apply_worker_loop(conn_factory, brief_path, profile_path) -> None:
    """Keeps the apply run advancing without anyone polling — the piece
    that makes Start actually mean 'walk away'. conn_factory is a
    zero-arg callable (web/app.py's _conn) so each iteration gets a
    fresh connection, matching the rest of the app's per-call pattern."""
    while True:
        conn = conn_factory()
        state = get_run_state(conn, "apply")
        if state["status"] == "running" and state["current_job_id"] is None:
            try:
                await apply_tick(conn, brief_path, profile_path)
            except Exception as exc:
                set_run_state(conn, "apply", status="error", current_job_id=None,
                              last_error=str(exc))
                store.log(conn, None, "run_error", str(exc))
            # a tick can return without awaiting anything (e.g. a guard
            # denial), so yield here rather than spinning the event loop
            await asyncio.sleep(0.1)
        else:
            await asyncio.sleep(1)


def queue_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) n FROM job j JOIN assessment a ON a.job_id = j.id"
        f" WHERE {QUEUE_WHERE}").fetchone()["n"]
