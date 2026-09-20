"""Chat data layer: conversations (one Home, one per job), their messages,
and the agent's open questions (agent_prompt). The React chat polls
messages_after(); the agent runner posts into it; nothing else writes here."""
import json
import sqlite3

_BACKFILL_MARK = "__backfilled__"


def _touch(conn, conversation_id: int) -> None:
    conn.execute("UPDATE conversation SET updated_at = datetime('now') WHERE id = ?",
                 (conversation_id,))


def home_conversation(conn) -> int:
    row = conn.execute("SELECT id FROM conversation WHERE kind = 'home'").fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO conversation (kind, title) VALUES ('home', 'Home')")
    conn.commit()
    return cur.lastrowid


def conversation_for_job(conn, job_id: int) -> int:
    row = conn.execute("SELECT id FROM conversation WHERE job_id = ?", (job_id,)).fetchone()
    if row:
        return row["id"]
    job = conn.execute("SELECT company, title FROM job WHERE id = ?", (job_id,)).fetchone()
    title = f"{job['company']} — {job['title']}" if job else f"Job #{job_id}"
    cur = conn.execute(
        "INSERT INTO conversation (kind, job_id, title) VALUES ('job', ?, ?)", (job_id, title))
    conn.commit()
    return cur.lastrowid


def post_message(conn, conversation_id: int, role: str, content: str,
                 payload: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO message (conversation_id, role, content, payload) VALUES (?, ?, ?, ?)",
        (conversation_id, role, content, json.dumps(payload) if payload is not None else None))
    _touch(conn, conversation_id)
    conn.commit()
    return cur.lastrowid


def _row_to_message(r) -> dict:
    return {"id": r["id"], "role": r["role"], "content": r["content"],
            "payload": json.loads(r["payload"]) if r["payload"] else None,
            "created_at": r["created_at"]}


def messages_after(conn, conversation_id: int, after_id: int = 0, limit: int = 200) -> list[dict]:
    if after_id:
        rows = conn.execute(
            "SELECT * FROM message WHERE conversation_id = ? AND id > ? ORDER BY id LIMIT ?",
            (conversation_id, after_id, limit)).fetchall()
    else:
        # A first load wants the NEWEST window: the oldest would leave a long
        # conversation painting old history for many 3 s polls.
        rows = conn.execute(
            "SELECT * FROM (SELECT * FROM message WHERE conversation_id = ?"
            " ORDER BY id DESC LIMIT ?) ORDER BY id", (conversation_id, limit)).fetchall()
    return [_row_to_message(r) for r in rows if r["content"] != _BACKFILL_MARK]


def list_conversations(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT c.id, c.kind, c.job_id, c.title, c.updated_at,"
        "  (SELECT content FROM message m WHERE m.conversation_id = c.id"
        "     AND m.content != ? ORDER BY m.id DESC LIMIT 1) AS last_message"
        " FROM conversation c ORDER BY c.updated_at DESC, c.id DESC",
        (_BACKFILL_MARK,)).fetchall()
    return [dict(r) for r in rows]


def jobs_needing_backfill(conn) -> list[int]:
    """Job ids with pre-chat history whose conversation has no marker yet.
    The conversations endpoint polls every 3s, so it asks this one question
    instead of asking backfill_job (a query per job) about every job."""
    rows = conn.execute(
        "SELECT DISTINCT h.job_id FROM ("
        "  SELECT job_id FROM application WHERE job_id IS NOT NULL"
        "  UNION SELECT job_id FROM event WHERE job_id IS NOT NULL) h"
        " LEFT JOIN conversation c ON c.job_id = h.job_id"
        " WHERE c.id IS NULL OR NOT EXISTS ("
        "   SELECT 1 FROM message m WHERE m.conversation_id = c.id AND m.content = ?)",
        (_BACKFILL_MARK,)).fetchall()
    return [r["job_id"] for r in rows]


def backfill_job(conn, job_id: int) -> int:
    """Render a job's pre-chat history (events, application rows) as system
    messages, once. The marker message is hidden by messages_after."""
    cid = conversation_for_job(conn, job_id)
    if conn.execute("SELECT 1 FROM message WHERE conversation_id = ? AND content = ?",
                    (cid, _BACKFILL_MARK)).fetchone():
        return 0
    n = 0
    for e in conn.execute("SELECT type, payload, occurred_at FROM event WHERE job_id = ?"
                          " ORDER BY id", (job_id,)):
        post_message(conn, cid, "system", f"{e['type']}: {e['payload'] or ''}".strip(": "),
                     {"occurred_at": e["occurred_at"]})
        n += 1
    for a in conn.execute("SELECT id, status, failure_reason, transcript_path FROM application"
                          " WHERE job_id = ? ORDER BY id", (job_id,)):
        reason = f" ({a['failure_reason']})" if a["failure_reason"] else ""
        post_message(conn, cid, "system", f"application #{a['id']}: {a['status']}{reason}",
                     {"application_id": a["id"], "transcript_path": a["transcript_path"]})
        n += 1
    conn.execute("INSERT INTO message (conversation_id, role, content) VALUES (?, 'system', ?)",
                 (cid, _BACKFILL_MARK))
    conn.commit()
    return n


def open_prompt(conn, job_id: int, kind: str, payload: dict) -> int:
    cid = conversation_for_job(conn, job_id)
    cur = conn.execute(
        "INSERT INTO agent_prompt (job_id, conversation_id, kind, payload) VALUES (?, ?, ?, ?)",
        (job_id, cid, kind, json.dumps(payload)))
    pid = cur.lastrowid
    post_message(conn, cid, "prompt", prompt_title(kind, payload), {"prompt_id": pid, "kind": kind})
    return pid


HOME_PROMPT_TTL = "-10 minutes"    # a Home approve card older than this can't be approved


def expire_stale_home_prompts(conn, conversation_id: int) -> None:
    conn.execute("UPDATE agent_prompt SET status = 'expired' WHERE conversation_id = ?"
                 " AND status = 'open' AND created_at < datetime('now', ?)",
                 (conversation_id, HOME_PROMPT_TTL))
    conn.commit()


def open_home_prompt(conn, kind: str, payload: dict) -> int:
    """A Home confirmation card (no job). One at a time: a newer request
    supersedes every older open one, so only the latest ask can be approved."""
    cid = home_conversation(conn)
    conn.execute("UPDATE agent_prompt SET status = 'expired'"
                 " WHERE conversation_id = ? AND status = 'open'", (cid,))
    cur = conn.execute(
        "INSERT INTO agent_prompt (job_id, conversation_id, kind, payload) VALUES (NULL, ?, ?, ?)",
        (cid, kind, json.dumps(payload)))
    pid = cur.lastrowid
    post_message(conn, cid, "prompt", prompt_title(kind, payload), {"prompt_id": pid, "kind": kind})
    return pid


def open_prompt_for_conversation(conn, conversation_id: int):
    return conn.execute("SELECT * FROM agent_prompt WHERE conversation_id = ? AND status = 'open'"
                        " ORDER BY id DESC LIMIT 1", (conversation_id,)).fetchone()


def prompt_title(kind: str, payload: dict) -> str:
    return "Review before applying" if kind == "confirm" else payload.get("question", kind)


def prompt_statuses(conn, messages: list[dict]) -> list[dict]:
    """Stamp each prompt message with its agent_prompt's current status."""
    ids = [m["payload"]["prompt_id"] for m in messages
           if m["role"] == "prompt" and (m["payload"] or {}).get("prompt_id")]
    if ids:
        status = dict(conn.execute(
            f"SELECT id, status FROM agent_prompt WHERE id IN ({','.join('?' * len(ids))})",
            ids).fetchall())
        for m in messages:
            if m["role"] == "prompt" and (m["payload"] or {}).get("prompt_id"):
                m["prompt_status"] = status.get(m["payload"]["prompt_id"], "expired")
    return messages


def open_prompt_for_job(conn, job_id: int):
    return conn.execute("SELECT * FROM agent_prompt WHERE job_id = ? AND status = 'open'"
                        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()


def open_prompt_for_any_job(conn):
    """The newest open job-scoped card, for a manual Apply/Override click --
    those never set run_state.current_job_id (that would park the worker on
    a job it didn't start), so the status bar has nothing to key off without
    this. job_id IS NOT NULL excludes Home's own cards, which carry none."""
    return conn.execute(
        "SELECT * FROM agent_prompt WHERE job_id IS NOT NULL AND status = 'open'"
        " ORDER BY id DESC LIMIT 1").fetchone()


def answer_prompt_row(conn, prompt_id: int, answer: dict):
    """The answered row, or None when this call did not answer it (already
    answered or expired) -- two answers racing must not both be relayed."""
    cur = conn.execute("UPDATE agent_prompt SET status = 'answered', answer = ?,"
                       " answered_at = datetime('now') WHERE id = ? AND status = 'open'",
                       (json.dumps(answer), prompt_id))
    conn.commit()
    if cur.rowcount != 1:
        return None
    return conn.execute("SELECT * FROM agent_prompt WHERE id = ?", (prompt_id,)).fetchone()


def reopen_prompt_row(conn, prompt_id: int, run_ended: bool) -> None:
    """Undo answer_prompt_row when the live run refused the answer: an answer
    the agent never received must not read as given. The card reopens only
    while its run is live (an in_flight row for the job): reopened after the
    run's expire_open_prompts it would be a zombie a later run could receive,
    so it is expired instead. The run's outcome is recorded before its cards
    expire, which closes the window between the two."""
    params = (prompt_id,)
    if not run_ended:
        conn.execute(
            "UPDATE agent_prompt SET status = 'open', answer = NULL, answered_at = NULL"
            " WHERE id = ? AND status = 'answered' AND EXISTS (SELECT 1 FROM application a"
            "   WHERE a.job_id = agent_prompt.job_id AND a.status = 'in_flight')", params)
    conn.execute("UPDATE agent_prompt SET status = 'expired', answer = NULL,"
                 " answered_at = NULL WHERE id = ? AND status = 'answered'", params)
    conn.commit()


def expire_open_prompts(conn, job_id: int) -> None:
    """A finished run's cards can never be answered."""
    conn.execute("UPDATE agent_prompt SET status = 'expired'"
                 " WHERE job_id = ? AND status = 'open'", (job_id,))
    conn.commit()
