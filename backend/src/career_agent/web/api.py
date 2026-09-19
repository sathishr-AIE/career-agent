"""JSON mirror of the Jinja routes in app.py, for the Vite/React frontend
(see the split-frontend plan). Every route here calls the same
career_agent.web.context / career_agent.web.actions functions the Jinja
routes call, so the two frontends read and write through one data path and
can't drift apart during the page-by-page migration.

career_agent.web.app imports this module's router (app.include_router) to
mount it, so importing app.py back at *module load* time here would be
circular. `_app()` defers that import to *call* time instead -- by the time
a request handler runs, app.py has finished importing, so the deferred
import is free and safe. It also keeps DB_PATH / BRIEF_PATH /
CANDIDATE_PROFILE_PATH a single source of truth: tests monkeypatch them on
the app module (`monkeypatch.setattr(web, "DB_PATH", ...)`), and reading
them through the app module here picks up that same patched value, exactly
like every Jinja route already does."""
from pathlib import Path

from fastapi import APIRouter, Body, File, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

from career_agent.apply import agent as agent_mod
from career_agent.web import actions, context

router = APIRouter(prefix="/api")


def _app():
    from career_agent.web import app as app_module
    return app_module


@router.get("/overview")
def api_overview():
    m = _app()
    return context.overview_context(m._conn(), m.BRIEF_PATH)


@router.get("/applications")
def api_applications(show: str = "queue"):
    m = _app()
    ctx = context.applications_context(
        m._conn(), show, m.BRIEF_PATH, scheduled=m.scheduled_task_installed())
    if show == "skipped":
        # `jobs` already holds every verdict here; `skipped_jobs` would send
        # the (large) skip list a second time in the same response.
        ctx.pop("skipped_jobs")
    return ctx


@router.get("/transcript/{application_id}")
def api_transcript(application_id: int):
    """The agent's redacted transcript for one application attempt -- so a
    failed/held row's chat can point somewhere other than the 10-line event
    log. Path is confined to LOG_DIR: an application_id is a plain integer,
    but the path it names came from a live run and is worth double-checking
    before it's read off disk and served."""
    m = _app()
    row = m._conn().execute(
        "SELECT transcript_path FROM application WHERE id = ?", (application_id,)).fetchone()
    if row is None or not row["transcript_path"]:
        return JSONResponse(status_code=404, content={"ok": False, "message": "No transcript"})
    path = Path(row["transcript_path"]).resolve()
    log_dir = agent_mod.LOG_DIR.resolve()
    if log_dir not in path.parents or not path.is_file():
        return JSONResponse(status_code=404, content={"ok": False, "message": "No transcript"})
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))


@router.get("/resumes")
def api_resumes():
    m = _app()
    return context.resumes_context(m._conn(), m.BRIEF_PATH)


@router.post("/resumes")
async def api_upload_resume(file: UploadFile = File(...)):
    result = await actions.upload_master_resume(file)
    return JSONResponse(status_code=200 if result["ok"] else 422, content=result)


@router.get("/settings")
def api_settings():
    m = _app()
    return context.settings_context(m._conn(), m.BRIEF_PATH, m.CANDIDATE_PROFILE_PATH)


_SETTINGS_DEFAULTS = {
    "target_titles": "", "title_families": "", "search_locations": "",
    "locations": "", "work_authorization": "", "excluded_companies": "",
    "non_negotiables": "", "remote_ok": False, "salary_floor_inr": "",
    "daily_cap": "", "gate_threshold": "", "staleness_days": "",
    "scoring_model": "", "max_score_per_run": "", "brief_present": False,
    "candidate_present": False, "candidate_name": "", "candidate_email": "",
    "candidate_phone": "", "linkedin_url": "", "portfolio_url": "",
    "apply_model": "",
}
_SETTINGS_NUMERIC_FIELDS = ("salary_floor_inr", "daily_cap", "gate_threshold",
                           "staleness_days", "max_score_per_run")


@router.put("/settings")
def api_settings_save(body: dict = Body(...)):
    """Body is the same field set the Jinja settings form posts (see
    actions.save_settings' docstring) as JSON instead of form-encoded --
    numbers and booleans are accepted either as JSON types or as strings and
    normalized to the strings actions.save_settings expects."""
    m = _app()
    conn = m._conn()
    form = {**_SETTINGS_DEFAULTS, **body}
    for field in _SETTINGS_NUMERIC_FIELDS:
        if form[field] is not None and not isinstance(form[field], str):
            form[field] = str(form[field])
    result = actions.save_settings(conn, form, m.BRIEF_PATH, m.CANDIDATE_PROFILE_PATH)
    if not result["ok"]:
        return JSONResponse(status_code=422, content=result)
    return context.settings_context(conn, m.BRIEF_PATH, m.CANDIDATE_PROFILE_PATH)


@router.put("/settings/models")
def api_settings_models(body: dict = Body(...)):
    """MS1: {scoring_model?, apply_model?} -- the chat composer's model picker."""
    return _result(actions.save_models(_app()._conn(), body.get("scoring_model"),
                                       body.get("apply_model")))


def _result(result: dict) -> JSONResponse:
    return JSONResponse(status_code=200 if result["ok"] else 422, content=result)


@router.post("/apply/{job_id}")
async def api_apply(job_id: int):
    m = _app()
    result = await actions.do_apply(
        m._conn(), job_id, allow_skip=False, event="human_applied",
        brief_path=m.BRIEF_PATH, candidate_profile_path=m.CANDIDATE_PROFILE_PATH,
        conn_factory=m._chat_conn)
    return _result(result)


@router.post("/override/{job_id}")
async def api_override(job_id: int):
    m = _app()
    result = await actions.do_apply(
        m._conn(), job_id, allow_skip=True, event="human_override",
        brief_path=m.BRIEF_PATH, candidate_profile_path=m.CANDIDATE_PROFILE_PATH,
        conn_factory=m._chat_conn)
    return _result(result)


@router.post("/answer/{job_id}")
def api_answer(job_id: int, question: str = Body(...), answer: str = Body(...),
              is_volatile: bool = Body(False)):
    m = _app()
    result = actions.answer_question(m._conn(), job_id, question, answer,
                                     is_volatile=is_volatile)
    return _result(result)


@router.post("/dismiss/{job_id}")
def api_dismiss(job_id: int):
    m = _app()
    return _result(actions.dismiss(m._conn(), job_id))


@router.post("/applied/{job_id}")
def api_mark_applied(job_id: int, when: str = Body("", embed=True)):
    m = _app()
    return _result(actions.mark_applied(m._conn(), job_id, when,
                                        brief_path=m.BRIEF_PATH))


@router.post("/outcome/{application_id}")
def api_record_outcome(application_id: int, type: str = Body(""),
                       occurred_at: str = Body(""), notes: str = Body("")):
    m = _app()
    return _result(actions.record_outcome(m._conn(), application_id, type,
                                          occurred_at, notes))


@router.get("/run/status")
def api_run_status():
    m = _app()
    return context.run_status_context(m._conn())


@router.post("/run/start")
async def api_run_start(mode: str = Body(..., embed=True)):
    m = _app()
    return _result(await actions.run_start(m._conn(), mode, m.BRIEF_PATH,
                                           m.CANDIDATE_PROFILE_PATH,
                                           conn_factory=m._chat_conn))


@router.post("/run/pause")
def api_run_pause():
    m = _app()
    return _result(actions.run_pause(m._conn()))


@router.post("/run/resume")
async def api_run_resume():
    m = _app()
    return _result(await actions.run_resume(m._conn(), m.BRIEF_PATH,
                                            m.CANDIDATE_PROFILE_PATH,
                                            conn_factory=m._chat_conn))


@router.post("/run/stop")
def api_run_stop():
    m = _app()
    return _result(actions.run_stop(m._conn()))


@router.post("/pipeline/run-now")
async def api_pipeline_run_now():
    """Same asyncio.create_task + _background_tasks bookkeeping as the
    Jinja route (app.pipeline_run_now) -- reusing app.py's own task registry
    here, via the deferred import, rather than a second one, so a run
    started from either frontend is tracked and cancelled the same way."""
    m = _app()
    result = actions.pipeline_run_now(m._conn(), m._conn, m.DB_PATH, m.BRIEF_PATH,
                                      m._background_tasks)
    return result if result["ok"] else JSONResponse(status_code=409, content=result)


@router.get("/pipeline/status")
def api_pipeline_status():
    m = _app()
    return context.pipeline_status_context(m._conn())


@router.post("/queue/{job_id}/skip")
async def api_queue_skip(job_id: int):
    m = _app()
    return _result(await actions.queue_skip(
        m._conn(), job_id, brief_path=m.BRIEF_PATH,
        candidate_profile_path=m.CANDIDATE_PROFILE_PATH,
        conn_factory=m._chat_conn))


@router.post("/queue/{job_id}/retry")
def api_queue_retry(job_id: int, confirm: bool = False):
    m = _app()
    return _result(actions.queue_retry(m._conn(), job_id,
                                       confirm_not_submitted=confirm))


@router.post("/queue/{job_id}/restore")
def api_queue_restore(job_id: int):
    m = _app()
    return _result(actions.queue_restore(m._conn(), job_id))


@router.post("/queue/{job_id}/priority")
def api_queue_priority(job_id: int, direction: str = Body(..., embed=True)):
    m = _app()
    return _result(actions.queue_priority(m._conn(), job_id, direction))
