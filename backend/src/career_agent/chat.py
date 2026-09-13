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
    rows = conn.execute(
        "SELECT * FROM message WHERE conversation_id = ? AND id > ? ORDER BY id LIMIT ?",
        (conversation_id, after_id, limit)).fetchall()
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
    post_message(conn, cid, "prompt", payload.get("question", kind), {"prompt_id": pid, "kind": kind})
    return pid


def open_prompt_for_job(conn, job_id: int):
    return conn.execute("SELECT * FROM agent_prompt WHERE job_id = ? AND status = 'open'"
                        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()


def answer_prompt_row(conn, prompt_id: int, answer: dict):
    conn.execute("UPDATE agent_prompt SET status = 'answered', answer = ?,"
                 " answered_at = datetime('now') WHERE id = ? AND status = 'open'",
                 (json.dumps(answer), prompt_id))
    conn.commit()
    return conn.execute("SELECT * FROM agent_prompt WHERE id = ?", (prompt_id,)).fetchone()
