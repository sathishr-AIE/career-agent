"""GET/DELETE /api/logins: the Logins drawer (slice S5). Metadata only --
credentials.list_ never returns a password or its ciphertext. Same
deferred-import rule as api_memory.py. Mounted in app.py."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from career_agent import credentials

router = APIRouter(prefix="/api")


def _app():
    from career_agent.web import app as app_module
    return app_module


@router.get("/logins")
def api_get_logins():
    return {"items": credentials.list_(_app()._conn())}


@router.delete("/logins/{login_id}")
def api_delete_login(login_id: int):
    if not credentials.delete(_app()._conn(), login_id):
        return JSONResponse(status_code=404,
                            content={"ok": False, "message": "No such login"})
    return {"ok": True, "message": "Deleted"}
