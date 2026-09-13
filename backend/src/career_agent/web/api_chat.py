"""/api/chat/*: the React chat's only backend. Same deferred-import rule as
api.py: app.py mounts this router, so app.py is imported at call time."""
import json
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse

from career_agent import chat
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
    open_prompt = None
    if conv and conv["job_id"]:
        row = chat.open_prompt_for_job(conn, conv["job_id"])
        if row:
            open_prompt = {"id": row["id"], "kind": row["kind"],
                           "payload": json.loads(row["payload"]), "created_at": row["created_at"]}
    return {"messages": chat.prompt_statuses(conn, chat.messages_after(conn, cid, after)),
            "open_prompt": open_prompt}


@router.get("/jobs/{job_id}/conversation")
def api_job_conversation(job_id: int):
    """So a page can open a job's chat (created on first use)."""
    return {"id": chat.conversation_for_job(_app()._conn(), job_id)}


@router.post("/{cid}/messages")
def api_post_message(cid: int, text: str = Body(..., embed=True)):
    conn = _app()._conn()
    conv = conn.execute("SELECT kind FROM conversation WHERE id = ?", (cid,)).fetchone()
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    stripped = text.strip()
    if not stripped:
        raise HTTPException(status_code=422, detail="empty message")
    mid = chat.post_message(conn, cid, "user", stripped)
    if conv["kind"] == "home":
        chat.post_message(conn, cid, "system", "Commands arrive in a later slice.")
    return {"ok": True, "message_id": mid}


@router.post("/prompts/{prompt_id}/answer")
def api_answer_prompt(prompt_id: int, answer: dict = Body(...)):
    """Body: {"answer": ..., "remember": bool} for an ASK card,
    {"decision": "approve"|"change"|"cancel", "changes": {...}} for CONFIRM.
    Refusals keep the {ok, message} body with 404/409/422."""
    m = _app()
    result = actions.answer_prompt(m._conn(), prompt_id, answer, m._chat_conn)
    return JSONResponse(status_code=200 if result["ok"] else result["code"], content=result)
