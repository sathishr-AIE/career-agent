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


def start(conn, job_id: int, session_id: str, nonce: str) -> None:
    conn.execute(
        "INSERT INTO apply_checkpoint (job_id, session_id, nonce, status) VALUES (?, ?, ?, 'running')"
        " ON CONFLICT(job_id) DO UPDATE SET session_id = excluded.session_id,"
        " nonce = excluded.nonce, step = 'start', answers = '{}', form_url = NULL,"
        " open_prompt_id = NULL, status = 'running', updated_at = datetime('now')",
        (job_id, session_id, nonce))
    conn.commit()


def mark_waiting(conn, job_id: int, prompt_id: int) -> None:
    _set(conn, job_id, "status = 'waiting', open_prompt_id = ?", prompt_id)


def mark_running(conn, job_id: int, step: str, answers: dict) -> None:
    _set(conn, job_id, "status = 'running', step = ?, open_prompt_id = NULL,"
         " answers = json_patch(answers, ?)", step, json.dumps(answers))


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


def sweep_orphans(conn, live_job_ids: set[int]) -> int:
    """running/waiting rows no live run is driving (a crashed server) -> resumable."""
    live = list(live_job_ids)
    cur = conn.execute(
        "UPDATE apply_checkpoint SET status = 'resumable', updated_at = datetime('now')"
        " WHERE status IN ('running','waiting')"
        f" AND job_id NOT IN ({','.join('?' * len(live))})", live)
    conn.commit()
    return cur.rowcount
