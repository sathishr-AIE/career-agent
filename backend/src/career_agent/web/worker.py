import asyncio
import functools
import logging
import sqlite3
from pathlib import Path

from career_agent import chat
from career_agent import run as run_module
from career_agent import store, tailor
from career_agent.apply import agent as agent_mod
from career_agent.apply import ats as ats_apply
from career_agent.apply import checkpoint
from career_agent.config import load_brief, load_candidate_profile

log = logging.getLogger(__name__)


def say(conn, job_id: int | None, text: str, conn_factory=None) -> None:
    """A lifecycle line in the job's chat (Home when job_id is None). Never
    raises: a chat write must not change what the worker does."""
    try:
        c = conn_factory() if conn_factory else conn
        try:
            cid = chat.home_conversation(c) if job_id is None else chat.conversation_for_job(c, job_id)
            chat.post_message(c, cid, "system", text)
        finally:
            if c is not conn:
                c.close()
    except Exception:
        log.warning("could not post %r for job %s", text, job_id, exc_info=True)


def _outcome_text(conn, job_id: int, result: dict) -> str:
    if result.get("status"):
        row = conn.execute("SELECT status, failure_reason FROM application WHERE job_id = ?"
                           " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        status, reason = (row["status"], row["failure_reason"]) if row else (result["status"], None)
        return f"Run ended: {status}" + (f" ({reason})" if reason else "")
    return f"Run ended: {result.get('reason', 'no outcome')}"

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
   -- A resumable stop leaves no application row (it consumes no attempt);
   -- this keeps it from being re-picked as a fresh job. Resume it instead.
   AND NOT EXISTS (
       SELECT 1 FROM apply_checkpoint cp
        WHERE cp.job_id = j.id AND cp.status = 'resumable'
   )
"""

CANDIDATE_SQL = f"""
SELECT j.id AS job_id, j.company, j.title, a.weighted_score
  FROM job j
  JOIN assessment a ON a.job_id = j.id
 WHERE {QUEUE_WHERE}
 ORDER BY j.priority ASC NULLS LAST, a.weighted_score DESC
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
    """The best queued job no request has claimed (ats_apply.PENDING)."""
    for row in conn.execute(CANDIDATE_SQL):
        if row["job_id"] not in ats_apply.PENDING:
            return row
    return None


def guard(conn: sqlite3.Connection, job_id: int, allow_skip: bool,
          brief_path: Path) -> str | None:
    """Dashboard-side guardrail. The partial unique index is the real
    guarantee; this exists to produce a readable message.

    Every denial returned here pauses the WHOLE run from apply_tick. Don't
    add a per-job denial without changing that branch, or one bad job
    stops the queue."""
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


async def apply_tick(conn: sqlite3.Connection, brief_path, profile_path,
                     conn_factory=None) -> None:
    """One step of the apply worker: pick a candidate, gate it, draft it,
    and in auto mode send it. Called by the control endpoints (for
    immediate feedback) and by the background loop (to keep going
    unattended). A no-op unless the apply run is 'running' and not parked
    on a needs-answer question. A draft never parks: its in-session
    CONFIRM was the review, so both modes move on to the next job."""
    state = get_run_state(conn, "apply")
    if state["status"] != "running":
        return
    if state["current_job_id"] is not None:
        return

    # Auto mode continues an interrupted session first, once per checkpoint until
    # a human touches it (the flag is on the row, so a restart can't loop).
    # Manual mode never does: the job chat offers a Continue button instead.
    resume_id = checkpoint.next_auto_resume(conn) if state["mode"] == "auto" else None
    candidate = None if resume_id is not None else next_candidate(conn)
    if resume_id is None and candidate is None:
        set_run_state(conn, "apply", status="idle", current_job_id=None)
        store.log(conn, None, "run_completed")
        say(conn, None, "Queue empty — apply run idle", conn_factory)
        return

    job_id = resume_id if resume_id is not None else candidate["job_id"]
    # Claimed for the whole tick, tailoring included: an Apply or a Continue on
    # this job is refused meanwhile, and next_candidate skips it.
    ats_apply.PENDING.add(job_id)
    try:
        await _tick_job(conn, state, job_id, resume_id, brief_path, profile_path, conn_factory)
    finally:
        ats_apply.PENDING.discard(job_id)


async def _tick_job(conn, state, job_id: int, resume_id, brief_path, profile_path,
                    conn_factory) -> None:
    set_run_state(conn, "apply", current_job_id=job_id)
    # Before any await: a stale needs_answer card answered while this job
    # starts would unpark it and let the next tick run it a second time.
    chat.expire_open_prompts(conn, job_id)
    say(conn, job_id, "Auto mode: continuing where it left off" if resume_id is not None
        else f"Picked up by the apply worker ({state['mode']} mode)", conn_factory)

    denial = guard(conn, job_id, allow_skip=True, brief_path=brief_path)
    if denial:
        # Pause on every denial: the job stays in the queue, so clearing
        # current_job_id alone would re-pick it on the next tick, forever.
        set_run_state(conn, "apply", status="paused", current_job_id=None)
        store.log(conn, job_id, "run_autopaused", denial)
        say(conn, job_id, f"Apply run auto-paused: {denial}", conn_factory)
        return
    if resume_id is not None:
        # After the guard (a denial must not use it up), before any await (no loop).
        checkpoint.set_auto_resumed(conn, job_id, True)

    try:
        resume_version = await tailor_for_apply(conn, job_id, brief_path)
    except Exception as exc:
        set_run_state(conn, "apply", status="error", current_job_id=None,
                      last_error=str(exc))
        store.log(conn, job_id, "run_error", str(exc))
        say(conn, job_id, f"Run error: {exc}", conn_factory)
        return

    brief = load_brief(brief_path)
    try:
        # load_candidate_profile raises FileNotFoundError if it's missing --
        # the agent needs it for every source now (submit() raises
        # RuntimeError on profile=None), so there's no fallback left: a
        # missing profile is a run_state error, caught below like any other
        # submit()-time failure.
        profile = load_candidate_profile(profile_path)
        # One session per job in both modes: manual CONFIRMs wait for the
        # human in the job chat, auto CONFIRMs are approved on the spot.
        result = await ats_apply.submit(conn, job_id, mode=state["mode"],
                                        brief=brief, profile=profile,
                                        resume_version=resume_version,
                                        conn_factory=conn_factory,
                                        **({"resume": True} if resume_id is not None else {}))
    except Exception as exc:
        set_run_state(conn, "apply", status="error", current_job_id=None,
                      last_error=str(exc))
        store.log(conn, job_id, "run_error", str(exc))
        say(conn, job_id, f"Run error: {exc}", conn_factory)
        return

    if result.get("needs_answer"):
        # Leave current_job_id set: apply_worker_loop's outer check
        # (current_job_id is None) keeps this exact job from being
        # re-picked on the next tick, so a question with no answer is a
        # stop, not a spin. Answering the text card submit() opened in the
        # job chat (actions.answer_prompt, origin needs_answer) clears it.
        store.log(conn, job_id, "needs_answer", result["needs_answer"])
        say(conn, job_id, "Run parked: waiting for your answer above", conn_factory)
        return

    if not result["ok"]:
        if result.get("unsupported"):
            # Real sends are refused outright while SUBMISSION_IMPLEMENTED
            # stays False, and that refusal writes no application row --
            # QUEUE_WHERE only excludes a job once one exists, so clearing
            # current_job_id here would let the very next tick re-pick this
            # same job and spin forever. Pause once instead, same as the
            # daily-cap path above: the run genuinely cannot proceed.
            set_run_state(conn, "apply", status="paused", current_job_id=None,
                          last_error=result.get("reason"))
            store.log(conn, job_id, "run_autopaused", result.get("reason", ""))
            say(conn, job_id, f"Apply run auto-paused: {result.get('reason', '')}", conn_factory)
            return
        store.log(conn, job_id, "job_skipped", result.get("reason", ""))
        set_run_state(conn, "apply", current_job_id=None)
        say(conn, job_id, _outcome_text(conn, job_id, result), conn_factory)
        return

    say(conn, job_id, _outcome_text(conn, job_id, result), conn_factory)
    set_run_state(conn, "apply", current_job_id=None)  # a draft does not park either


def startup_sweep(conn: sqlite3.Connection) -> None:
    """Crash recovery. app.lifespan runs it on a plain connection before the first
    _conn(), whose sweep_stale_in_flight would otherwise hold a crashed run's row
    first (and sweep_orphans would then read that hold as an ended job).

    A checkpoint left running/waiting by a crashed server becomes resumable, or
    done when it could have sent (checkpoint.sweep_orphans). A crashed run's
    in_flight row is dropped -- after the sweep, which reads that row as "not
    ended" -- when nothing could have been sent: always with the kill switch
    off, and with it on only for a session swept resumable. Any other row stays
    for held_unknown adjudication. Each interrupted job's chat, and Home, say so."""
    live = {jid for jid, run in agent_mod.RUNS.items() if not run.done.is_set()}
    orphans = [r["job_id"] for r in conn.execute(
        "SELECT job_id FROM apply_checkpoint WHERE status IN ('running','waiting')"
        " ORDER BY job_id") if r["job_id"] not in live]
    checkpoint.sweep_orphans(conn, live)
    resumable = {r["job_id"] for r in conn.execute(
        "SELECT job_id FROM apply_checkpoint WHERE status = 'resumable'")}
    for r in conn.execute("SELECT id, job_id FROM application"
                          " WHERE status = 'in_flight'").fetchall():
        if r["job_id"] in live or (ats_apply.SUBMISSION_IMPLEMENTED and r["job_id"] not in resumable):
            continue
        if conn.execute("DELETE FROM application WHERE id = ? AND status = 'in_flight'",
                        (r["id"],)).rowcount:
            store.log(conn, r["job_id"], "orphan_in_flight_dropped",
                      "parked on a card: nothing was sent" if ats_apply.SUBMISSION_IMPLEMENTED
                      else "submission disabled: nothing was sent")
    conn.commit()
    for jid in orphans:
        say(conn, jid, "The server restarted while this job was running — "
            + ("press Continue to pick it up" if jid in resumable else "it was held for review"))
    if orphans:
        say(conn, None, f"{len(orphans)} job(s) interrupted by a restart: "
            + ", ".join(f"#{j}" for j in orphans) + " — open each chat to continue")


async def apply_worker_loop(conn_factory, brief_path, profile_path,
                            chat_conn_factory=None) -> None:
    """Keeps the apply run advancing without anyone polling — the piece
    that makes Start actually mean 'walk away'. conn_factory is a
    zero-arg callable (web/app.py's _conn) so each iteration gets a
    fresh connection, matching the rest of the app's per-call pattern.
    chat_conn_factory (default: conn_factory) is the lighter one handed to
    submit() for the agent's narration (see ats._chat_events)."""
    # No startup_sweep here: app.lifespan runs it once at boot, before any
    # _conn(). By now a request may have started a submit() whose in_flight
    # row exists but whose run is not yet in RUNS -- a sweep would drop it.
    while True:
        conn = conn_factory()
        state = get_run_state(conn, "apply")
        if state["status"] == "running" and state["current_job_id"] is None:
            try:
                await apply_tick(conn, brief_path, profile_path,
                                 chat_conn_factory or conn_factory)
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
