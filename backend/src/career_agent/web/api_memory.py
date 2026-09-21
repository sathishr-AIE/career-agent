"""GET/PUT/DELETE /api/memory: the qa_bank editor (slice S4 --
personalized agent memory). Same deferred-import rule as api.py/
api_chat.py. Mounted in app.py."""
from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from career_agent import store
from career_agent.apply.ats import QA_VOLATILE_WINDOW_DAYS

router = APIRouter(prefix="/api")


def _app():
    from career_agent.web import app as app_module
    return app_module


@router.get("/memory")
def api_get_memory():
    # The window the page labels the re-confirm chip with, from the same
    # constant apply/agent.py marks a volatile answer stale by (MM1).
    return {"items": store.memory_list(_app()._conn()),
            "volatile_window_days": QA_VOLATILE_WINDOW_DAYS}


@router.put("/memory/{item_id}")
def api_put_memory(item_id: int, body: dict = Body(...)):
    answer = str(body.get("answer") or "").strip()
    if not answer:
        return JSONResponse(status_code=422,
                            content={"ok": False, "message": "The answer can't be empty"})
    conn = _app()._conn()
    if not store.qa_update(conn, item_id, answer, bool(body.get("is_volatile"))):
        return JSONResponse(status_code=404,
                            content={"ok": False, "message": "No such memory"})
    return {"ok": True, "message": "Saved"}


@router.delete("/memory/{item_id}")
def api_delete_memory(item_id: int):
    conn = _app()._conn()
    if not store.qa_delete(conn, item_id):
        return JSONResponse(status_code=404,
                            content={"ok": False, "message": "No such memory"})
    return {"ok": True, "message": "Deleted"}
