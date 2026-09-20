"""GET/POST/PUT/DELETE /api/facts: the facts store editor. The gate and the
tailor refuse to run below gate.MIN_FACTS_HARD facts, so this is how a user
gets past that. Same deferred-import rule as api_memory.py. Mounted in app.py."""
from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from career_agent import store
from career_agent.gate import MIN_FACTS_HARD, MIN_FACTS_WARN

router = APIRouter(prefix="/api")


def _app():
    from career_agent.web import app as app_module
    return app_module


def _invalid(errors: dict) -> JSONResponse:
    return JSONResponse(status_code=422, content={
        "ok": False, "message": "; ".join(errors.values()), "errors": errors})


@router.get("/facts")
def api_get_facts():
    conn = _app()._conn()
    # FC1: who cites what, so the page can show it before a delete is refused
    # for it. Merged here rather than in fact_list, which stays a single-table
    # read.
    cited = store.fact_citations(conn)
    return {"items": [{**f, "cited_by": cited.get(f["id"], [])}
                      for f in store.fact_list(conn)],
            "min_hard": MIN_FACTS_HARD, "min_warn": MIN_FACTS_WARN}


@router.post("/facts")
def api_add_fact(body: dict = Body(...)):
    row, errors = store.fact_validate(body)
    if errors:
        return _invalid(errors)
    return {"ok": True, "message": "Added", "id": store.fact_add(_app()._conn(), row)}


@router.put("/facts/{fact_id}")
def api_put_fact(fact_id: int, body: dict = Body(...)):
    row, errors = store.fact_validate(body)
    if errors:
        return _invalid(errors)
    if not store.fact_update(_app()._conn(), fact_id, row):
        return JSONResponse(status_code=404, content={"ok": False, "message": "No such fact"})
    return {"ok": True, "message": "Saved"}


@router.delete("/facts/{fact_id}")
def api_delete_fact(fact_id: int):
    conn = _app()._conn()
    cited = store.fact_cited_by(conn, fact_id)
    if cited:
        # Deleting it would turn those resumes' bullet citations into "unknown fact".
        return JSONResponse(status_code=409, content={
            "ok": False, "message": f"Cited by tailored resume {', '.join(cited)}; edit it instead"})
    if not store.fact_delete(conn, fact_id):
        return JSONResponse(status_code=404, content={"ok": False, "message": "No such fact"})
    return {"ok": True, "message": "Deleted"}
