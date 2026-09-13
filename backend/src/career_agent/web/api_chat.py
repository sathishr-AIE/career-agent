"""/api/chat/*: the React chat's only backend. Same deferred-import rule as
api.py: app.py mounts this router, so app.py is imported at call time."""
import json
from fastapi import APIRouter, Body

from career_agent import chat

router = APIRouter(prefix="/api/chat")


def _app():
    from career_agent.web import app as app_module
    return app_module


@router.get("/conversations")
def api_conversations():
    conn = _app()._conn()
    home_id = chat.home_conversation(conn)
    for r in conn.execute("SELECT DISTINCT job_id FROM ("
                          "SELECT job_id FROM application UNION SELECT job_id FROM event"
                          " WHERE job_id IS NOT NULL)"):
        chat.backfill_job(conn, r["job_id"])
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
    return {"messages": chat.messages_after(conn, cid, after), "open_prompt": open_prompt}


@router.post("/{cid}/messages")
def api_post_message(cid: int, text: str = Body(..., embed=True)):
    conn = _app()._conn()
    mid = chat.post_message(conn, cid, "user", text.strip())
    conv = conn.execute("SELECT kind FROM conversation WHERE id = ?", (cid,)).fetchone()
    if conv and conv["kind"] == "home":
        chat.post_message(conn, cid, "system", "Commands arrive in a later slice.")
    return {"ok": True, "message_id": mid}
