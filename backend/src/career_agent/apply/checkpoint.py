"""Per-job apply checkpoints (spec S3): where an interrupted run stopped, so
it can `--resume` the same session instead of starting over.

One row per job. `resumable` is also what keeps a stopped job out of the
apply queue without an application row (see worker.QUEUE_WHERE): a resumable
stop consumes no attempt. Every write commits."""
import json

RESUMED_TEXT = ("You were interrupted. The page may have reloaded. If you were waiting on an "
                "ASK or CONFIRM, emit it again now; previously answered questions are in "
                "PREVIOUSLY ANSWERED.")


def _set(conn, job_id: int, sql: str, *args) -> None:
    conn.execute(f"UPDATE apply_checkpoint SET {sql}, updated_at = datetime('now')"
                 " WHERE job_id = ?", (*args, job_id))
    conn.commit()


def start(conn, job_id: int, session_id: str, nonce: str, mode: str | None = None,
          can_submit: bool | None = None, resume_count: int = 0) -> None:
    """A fresh session: everything resets, the counters included (resume_count
    only carries over a mode-mismatch restart). mode and can_submit are what a
    resume must match (see ats.submit)."""
    conn.execute(
        "INSERT INTO apply_checkpoint (job_id, session_id, nonce, status, mode, can_submit,"
        " resume_count) VALUES (?, ?, ?, 'running', ?, ?, ?)"
        " ON CONFLICT(job_id) DO UPDATE SET session_id = excluded.session_id,"
        " nonce = excluded.nonce, step = 'start', answers = '{}', form_url = NULL,"
        " open_prompt_id = NULL, status = 'running', mode = excluded.mode,"
        " can_submit = excluded.can_submit, approve_sent = 0, resume_count = excluded.resume_count,"
        " auto_resumed = 0, updated_at = datetime('now')",
        (job_id, session_id, nonce, mode, None if can_submit is None else int(can_submit),
         resume_count))
    conn.commit()


def restart(conn, job_id: int, session_id: str, nonce: str) -> None:
    """A resume fallback's fresh session: new session id and nonce, but the same
    answers, mode, and resume count -- a fallback must not reset the cap."""
    _set(conn, job_id, "session_id = ?, nonce = ?, status = 'running',"
         " step = 'resume_fallback', open_prompt_id = NULL", session_id, nonce)


def mark_waiting(conn, job_id: int, prompt_id: int) -> None:
    _set(conn, job_id, "status = 'waiting', open_prompt_id = ?", prompt_id)


def mark_running(conn, job_id: int, step: str, answers: dict) -> None:
    """Only a live run's row moves: an answer recorded after the run already
    finished (or stopped resumable) must not reopen it."""
    conn.execute("UPDATE apply_checkpoint SET status = 'running', step = ?, open_prompt_id = NULL,"
                 " answers = json_patch(answers, ?), updated_at = datetime('now')"
                 " WHERE job_id = ? AND status IN ('running','waiting')",
                 (step, json.dumps(answers), job_id))
    conn.commit()


def resume(conn, job_id: int) -> None:
    """resumable -> running, for submit(resume=True). Counts toward ats.MAX_RESUMES."""
    _set(conn, job_id, "status = 'running', step = 'resumed', resume_count = resume_count + 1")


def restore(conn, job_id: int, cp: dict) -> None:
    """Undo mark_running for an answer the run never received."""
    conn.execute("UPDATE apply_checkpoint SET status = ?, step = ?, open_prompt_id = ?, answers = ?,"
                 " updated_at = datetime('now') WHERE job_id = ? AND status = 'running'",
                 (cp["status"], cp["step"], cp["open_prompt_id"], json.dumps(cp["answers"]), job_id))
    conn.commit()


def mark_approve_sent(conn, job_id: int) -> None:
    """A DECISION approve is going out: this session may click Submit, so a
    crash must never make it resumable (sweep_orphans)."""
    _set(conn, job_id, "approve_sent = 1")


def set_auto_resumed(conn, job_id: int, flag: bool) -> None:
    """The worker's one auto-resume per checkpoint; a human Continue re-arms it."""
    _set(conn, job_id, "auto_resumed = ?", int(flag))


def release_claim(conn, job_id: int) -> None:
    """Undo a human Continue's claim -- only while it is still that claim: a
    checkpoint the worker's own session has since taken is left alone."""
    conn.execute("UPDATE apply_checkpoint SET auto_resumed = 0, updated_at = datetime('now')"
                 " WHERE job_id = ? AND status = 'resumable' AND auto_resumed = 1", (job_id,))
    conn.commit()


def next_auto_resume(conn, skip=frozenset()) -> int | None:
    """Only an auto session: a manual one (an "Apply anyway" on a gate skip
    included) restarted by the auto worker would submit what nobody reviewed."""
    for row in conn.execute("SELECT job_id FROM apply_checkpoint WHERE status = 'resumable'"
                            " AND mode = 'auto' AND auto_resumed = 0"
                            " ORDER BY updated_at, job_id"):
        if row["job_id"] not in skip:
            return row["job_id"]
    return None


def mark_resumable(conn, job_id: int) -> None:
    _set(conn, job_id, "status = 'resumable'")


def finish(conn, job_id: int) -> None:
    _set(conn, job_id, "status = 'done', open_prompt_id = NULL")


def get(conn, job_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM apply_checkpoint WHERE job_id = ?", (job_id,)).fetchone()
    return None if row is None else {**dict(row), "answers": json.loads(row["answers"])}


def continue_message(cp: dict, nonce: str) -> str:
    body = json.dumps({"step": cp["step"], "answers": cp["answers"]}, ensure_ascii=False)
    return f"CONTINUE:{nonce}:{body}\n{RESUMED_TEXT}"


# A latest application row in one of these means the run reached an outcome
# (a lost finish() write, or a crash the in_flight sweep already held).
_TERMINAL = ("submitted", "draft", "failed", "failed_permanent", "held_unknown")


def sweep_orphans(conn, live_job_ids: set[int]) -> int:
    """running/waiting rows no live run is driving (a crashed server) -> resumable,
    or done when the job's latest application is terminal, or when the session could
    submit and may have: it sent a DECISION approve, it was not a manual session
    (an auto session is pre-approved and can click Submit before approve_sent is
    written; a NULL mode is a legacy row, treated the same), or it crashed mid-turn
    (`running`: the prompt's "no Submit without approve" is all that stood between
    an injected page and Submit). Only a `waiting` manual session with no approve
    is safe. Resuming the others risks a double submit; their in_flight row goes
    through held_unknown. Returns the resumable count."""
    live = list(live_job_ids)
    orphan = (" WHERE status IN ('running','waiting')"
              f" AND job_id NOT IN ({','.join('?' * len(live))})")
    ended = (" AND ((can_submit IS NOT 0 AND (approve_sent = 1 OR mode IS NOT 'manual'"
             " OR status = 'running'))"
             " OR (SELECT ap.status FROM application ap WHERE ap.job_id = apply_checkpoint.job_id"
             f" ORDER BY ap.id DESC LIMIT 1) IN ({','.join('?' * len(_TERMINAL))}))")
    conn.execute("UPDATE apply_checkpoint SET status = 'done', open_prompt_id = NULL,"
                 " updated_at = datetime('now')" + orphan + ended, (*live, *_TERMINAL))
    cur = conn.execute("UPDATE apply_checkpoint SET status = 'resumable',"
                       " updated_at = datetime('now')" + orphan, live)
    conn.commit()
    return cur.rowcount
