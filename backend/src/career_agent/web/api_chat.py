"""/api/chat/*: the React chat's only backend. Same deferred-import rule as
api.py: app.py mounts this router, so app.py is imported at call time."""
import json
from fastapi import APIRouter, Body, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from career_agent import chat
from career_agent.apply import ats as ats_apply
from career_agent.apply import checkpoint
from career_agent.web import actions

router = APIRouter(prefix="/api/chat")


def _app():
    from career_agent.web import app as app_module
    return app_module


@router.get("/conversations")
def api_conversations():
    conn = _app()._conn()
    home_id = chat.home_conversation(conn)
    for job_id in chat.jobs_needing_backfill(conn):
        chat.backfill_job(conn, job_id)
    return {"conversations": chat.list_conversations(conn), "home_id": home_id}


@router.get("/{cid}/messages")
def api_messages(cid: int, after: int = 0):
    conn = _app()._conn()
    conv = conn.execute("SELECT job_id FROM conversation WHERE id = ?", (cid,)).fetchone()
    open_prompt, resumable, row = None, False, None
    if conv and conv["job_id"]:
        cp = checkpoint.get(conn, conv["job_id"])
        # Never offer a Continue that is certain to be refused (a BLOCKING attempt).
        resumable = bool(cp and cp["status"] == "resumable"
                         and not ats_apply._blocking_status(conn, conv["job_id"]))
        row = chat.open_prompt_for_job(conn, conv["job_id"])
    elif conv:     # Home: its confirmation cards carry no job
        chat.expire_stale_home_prompts(conn, cid)
        row = chat.open_prompt_for_conversation(conn, cid)
    if row:
        open_prompt = {"id": row["id"], "kind": row["kind"],
                       "payload": json.loads(row["payload"]), "created_at": row["created_at"]}
    return {"messages": chat.prompt_statuses(conn, chat.messages_after(conn, cid, after)),
            "open_prompt": open_prompt, "resumable": resumable}


@router.post("/jobs/{job_id}/resume")
async def api_resume_job(job_id: int):
    """Continue where it left off. Refusals keep the {ok, message} body with 409."""
    m = _app()
    result = actions.resume_job(m._conn(), job_id, m.BRIEF_PATH, m.CANDIDATE_PROFILE_PATH,
                                m._chat_conn, m._background_tasks)
    return JSONResponse(status_code=200 if result["ok"] else result["code"], content=result)


@router.get("/jobs/{job_id}/conversation")
def api_job_conversation(job_id: int):
    """So a page can open a job's chat (created on first use)."""
    return {"id": chat.conversation_for_job(_app()._conn(), job_id)}


@router.post("/{cid}/messages")
async def api_post_message(cid: int, text: str = Body(..., embed=True)):
    """A Home message is routed (off the event loop) and answered there; a
    job chat message is only recorded."""
    m = _app()
    conn = m._conn()
    conv = conn.execute("SELECT kind FROM conversation WHERE id = ?", (cid,)).fetchone()
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    stripped = text.strip()
    if not stripped:
        raise HTTPException(status_code=422, detail="empty message")
    if conv["kind"] == "home":
        return await actions.home_message(conn, stripped, m.BRIEF_PATH, m.CANDIDATE_PROFILE_PATH,
                                          m._chat_conn, tasks=m._background_tasks)
    return {"ok": True, "message_id": chat.post_message(conn, cid, "user", stripped)}


@router.post("/prompts/{prompt_id}/answer")
async def api_answer_prompt(prompt_id: int, answer: dict = Body(...)):
    """Body: {"answer": ..., "remember": bool} for an ASK card,
    {"decision": "approve"|"change"|"cancel", "changes": {...}} for CONFIRM.
    Refusals keep the {ok, message} body with 404/409/422."""
    m = _app()
    probe = m._chat_conn()
    try:
        row = probe.execute("SELECT * FROM agent_prompt WHERE id = ?", (prompt_id,)).fetchone()
        home = row is not None and actions.is_home_prompt(probe, row)
    finally:
        probe.close()
    if home:    # on the loop: an approved Home card starts its work as a background task
        result = actions.answer_prompt(m._conn(), prompt_id, answer, m._chat_conn,
                                       brief_path=m.BRIEF_PATH, profile_path=m.CANDIDATE_PROFILE_PATH,
                                       db_path=m.DB_PATH, tasks=m._background_tasks,
                                       run_conn_factory=m._conn)
    else:       # off the loop: a live run's answer does blocking work; the conn is made in the thread
        result = await run_in_threadpool(
            lambda: actions.answer_prompt(m._conn(), prompt_id, answer, m._chat_conn))
    return JSONResponse(status_code=200 if result["ok"] else result["code"], content=result)
