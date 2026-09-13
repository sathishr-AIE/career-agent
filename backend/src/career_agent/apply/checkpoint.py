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
          can_submit: bool | None = None) -> None:
    """A fresh session: everything resets, counters included. mode and
    can_submit are what a resume must match (see ats.submit)."""
    conn.execute(
        "INSERT INTO apply_checkpoint (job_id, session_id, nonce, status, mode, can_submit)"
        " VALUES (?, ?, ?, 'running', ?, ?)"
        " ON CONFLICT(job_id) DO UPDATE SET session_id = excluded.session_id,"
        " nonce = excluded.nonce, step = 'start', answers = '{}', form_url = NULL,"
        " open_prompt_id = NULL, status = 'running', mode = excluded.mode,"
        " can_submit = excluded.can_submit, approve_sent = 0, resume_count = 0,"
        " auto_resumed = 0, updated_at = datetime('now')",
        (job_id, session_id, nonce, mode, None if can_submit is None else int(can_submit)))
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


def mark_approve_sent(conn, job_id: int) -> None:
    """A DECISION approve is going out: this session may click Submit, so a
    crash must never make it resumable (sweep_orphans)."""
    _set(conn, job_id, "approve_sent = 1")


def set_auto_resumed(conn, job_id: int, flag: bool) -> None:
    """The worker's one auto-resume per checkpoint; a human Continue re-arms it."""
    _set(conn, job_id, "auto_resumed = ?", int(flag))


def next_auto_resume(conn) -> int | None:
    row = conn.execute("SELECT job_id FROM apply_checkpoint WHERE status = 'resumable'"
                       " AND auto_resumed = 0 ORDER BY updated_at, job_id LIMIT 1").fetchone()
    return row["job_id"] if row else None


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
    or done when the job's latest application is terminal or the session sent a
    DECISION approve while it could submit (resuming it risks a double submit; its
    in_flight row goes through held_unknown). Returns the resumable count."""
    live = list(live_job_ids)
    orphan = (" WHERE status IN ('running','waiting')"
              f" AND job_id NOT IN ({','.join('?' * len(live))})")
    ended = (" AND ((approve_sent = 1 AND can_submit IS NOT 0)"
             " OR (SELECT ap.status FROM application ap WHERE ap.job_id = apply_checkpoint.job_id"
             f" ORDER BY ap.id DESC LIMIT 1) IN ({','.join('?' * len(_TERMINAL))}))")
    conn.execute("UPDATE apply_checkpoint SET status = 'done', open_prompt_id = NULL,"
                 " updated_at = datetime('now')" + orphan + ended, (*live, *_TERMINAL))
    cur = conn.execute("UPDATE apply_checkpoint SET status = 'resumable',"
                       " updated_at = datetime('now')" + orphan, live)
    conn.commit()
    return cur.rowcount
