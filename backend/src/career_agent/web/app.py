import asyncio
import datetime as dt  # re-exported as web.dt -- tests monkeypatch datetime through it
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from career_agent import db, store, tailor  # tailor re-exported as web.tailor -- see below
from career_agent.apply import ats as ats_apply
from career_agent.web import actions, context, pipeline, worker
from career_agent.web.api import router as api_router

# tailor and dt aren't used directly in this module anymore (both moved into
# context.py/actions.py), but tests monkeypatch them as web.tailor/web.dt --
# e.g. monkeypatch.setattr(web.tailor, "TEMPLATE_PATH", ...) -- and that
# works precisely because a module is a singleton: patching an attribute on
# the `tailor` name bound here mutates the same career_agent.tailor module
# object that context.py and actions.py imported too. Dropping the import
# would only remove the name test_web.py reaches through, not the sharing.

# Moved to context.py, aliased here so test_web.py's direct call
# (web._run_status_context(conn)) keeps working unchanged.
_run_status_context = context.run_status_context


def scheduled_task_installed(name: str = "CareerAgentDaily") -> bool:
    """No trigger is installed by default. The dashboard says so rather than
    letting you assume something ran overnight when nothing did.

    Deliberately stays a function defined here, not moved into context.py:
    test_web.py monkeypatches it as web.scheduled_task_installed, and a
    caller inside context.py would keep resolving its own module-level name
    regardless of what gets rebound on this one -- rebinding an alias
    doesn't change what the original definition's call sites see. So
    applications_context takes the result as a `scheduled` argument instead
    of calling this itself; whichever route builds that context (Jinja or
    JSON) calls this function -- through whichever name is currently bound
    to it -- and passes the answer in."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-ScheduledTask -TaskName {name} -ErrorAction SilentlyContinue"],
            capture_output=True, text=True, timeout=10)
        return name in result.stdout
    except Exception:
        return False

DB_PATH = Path("data/career.db")
BRIEF_PATH = Path("career_brief.toml")
CANDIDATE_PROFILE_PATH = Path("candidate_profile.toml")


# asyncio only holds a weak reference to a running task, so a fire-and-forget
# create_task can be garbage-collected mid-run. Tasks live here until done.
_background_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Crash recovery first, on a plain connection: _conn() runs
    # sweep_stale_in_flight, which would hold a crashed run's in_flight row
    # before worker.startup_sweep can drop it.
    boot = db.connect(DB_PATH)
    try:
        db.init_schema(boot)
        worker.startup_sweep(boot)
    finally:
        boot.close()
    conn = _conn()
    # There is no pause/resume/stop for the pipeline by design, so a 'running'
    # row left by a process that died mid-run would strand the Run Now button
    # on a disabled "Running…" forever. A fresh process can only ever find it
    # that way after a crash or restart, so clear it.
    if worker.get_run_state(conn, "pipeline")["status"] == "running":
        worker.set_run_state(conn, "pipeline", status="error",
                             last_error="Interrupted by a server restart.")
    task = asyncio.create_task(
        worker.apply_worker_loop(_conn, BRIEF_PATH, CANDIDATE_PROFILE_PATH,
                                 chat_conn_factory=_chat_conn))
    yield
    task.cancel()


app = FastAPI(title="Career Agent", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.include_router(api_router)
from career_agent.web import api_chat, api_credentials, api_facts, api_memory, api_profile
for _api in (api_chat, api_memory, api_credentials, api_profile, api_facts):
    app.include_router(_api.router)


def _conn():
    conn = db.connect(DB_PATH)
    db.init_schema(conn)
    ats_apply.sweep_stale_in_flight(conn)
    return conn


def _chat_conn():
    """The agent's narration factory: called per event on the thread that
    drains `claude`'s stdout, so no init_schema/sweep there -- a slow commit
    stalls the agent on its pipe. DB_PATH read at call time (tests patch it)."""
    return db.connect(DB_PATH)


def _span(result: dict) -> HTMLResponse:
    """Every action route (Apply, Send, Skip, ...) resolves to the same
    shape -- {"ok": bool, "message": str} -- from career_agent.web.actions.
    This is the one place that turns it into the <span> the templates and
    their htmx swaps expect. actions.py hands back plain text, so escaping
    happens here, once, rather than per action."""
    cls = "done" if result["ok"] else "denied"
    return HTMLResponse(f'<span class="{cls}">{escape(result["message"])}</span>')


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = _conn()
    return templates.TemplateResponse(
        request=request, name="overview.html",
        context={"active_nav": "dashboard",
                 **context.overview_context(conn, BRIEF_PATH)})


@app.get("/resume/{version}")
def download_resume(version: str):
    conn = _conn()
    row = conn.execute("SELECT path FROM resume WHERE version = ?",
                       (version,)).fetchone()
    if row is None or not Path(row["path"]).exists():
        return HTMLResponse("Resume not found", status_code=404)
    return FileResponse(row["path"], filename=Path(row["path"]).name)


@app.get("/resumes", response_class=HTMLResponse)
async def resumes_page(request: Request):
    conn = _conn()
    return templates.TemplateResponse(
        request=request, name="resumes.html",
        context={"active_nav": "resumes",
                 **context.resumes_context(conn, BRIEF_PATH)})


@app.post("/resumes/master", response_class=HTMLResponse)
async def upload_master_resume(file: UploadFile = File(...)):
    result = await actions.upload_master_resume(file)
    if not result["ok"] or not result.get("changes"):
        return _span(result)
    detail = "".join(f"<li>{escape(c)}</li>" for c in result["changes"])
    return HTMLResponse(
        f'<span class="done">{escape(result["message"])}'
        f'<ul>{detail}</ul></span>')


@app.get("/applications", response_class=HTMLResponse)
def applications(request: Request, show: str = "queue"):
    conn = _conn()
    return templates.TemplateResponse(
        request=request, name="applications.html",
        context={"active_nav": "applications",
                 **context.applications_context(
                     conn, show, BRIEF_PATH,
                     scheduled=scheduled_task_installed())})


@app.post("/apply/{job_id}", response_class=HTMLResponse)
async def apply(job_id: int):
    conn = _conn()
    return _span(await actions.do_apply(
        conn, job_id, allow_skip=False, event="human_applied",
        brief_path=BRIEF_PATH, candidate_profile_path=CANDIDATE_PROFILE_PATH,
        conn_factory=_chat_conn))


@app.post("/override/{job_id}", response_class=HTMLResponse)
async def override(job_id: int):
    """Applying to something the gate skipped. The most valuable label the
    system produces, because it is the gate erring in the expensive direction."""
    conn = _conn()
    return _span(await actions.do_apply(
        conn, job_id, allow_skip=True, event="human_override",
        brief_path=BRIEF_PATH, candidate_profile_path=CANDIDATE_PROFILE_PATH,
        conn_factory=_chat_conn))


@app.post("/answer/{job_id}", response_class=HTMLResponse)
def answer_question(job_id: int, question: str = Form(...),
                    answer: str = Form(...),
                    is_volatile: str | None = Form(None)):
    conn = _conn()
    return _span(actions.answer_question(
        conn, job_id, question, answer, is_volatile=bool(is_volatile)))


@app.post("/dismiss/{job_id}", response_class=HTMLResponse)
def dismiss(job_id: int):
    conn = _conn()
    return _span(actions.dismiss(conn, job_id))


@app.post("/applied/{job_id}", response_class=HTMLResponse)
def mark_applied(job_id: int, when: str = Form("")):
    conn = _conn()
    return _span(actions.mark_applied(conn, job_id, when, brief_path=BRIEF_PATH))


@app.post("/outcome/{application_id}", response_class=HTMLResponse)
def record_outcome(application_id: int, type: str = Form(""),
                   occurred_at: str = Form(""), notes: str = Form("")):
    conn = _conn()
    return _span(actions.record_outcome(conn, application_id, type,
                                        occurred_at, notes))


@app.get("/run/status", response_class=HTMLResponse)
def run_status(request: Request):
    conn = _conn()
    return templates.TemplateResponse(
        request=request, name="_run_status.html",
        context=context.run_status_context(conn))


@app.post("/run/start")
async def run_start(mode: str = Form(...)):
    conn = _conn()
    await actions.run_start(conn, mode, BRIEF_PATH, CANDIDATE_PROFILE_PATH,
                            conn_factory=_chat_conn)
    return HTMLResponse("ok")


@app.post("/run/pause")
def run_pause():
    conn = _conn()
    actions.run_pause(conn)
    return HTMLResponse("ok")


@app.post("/run/resume")
async def run_resume():
    conn = _conn()
    await actions.run_resume(conn, BRIEF_PATH, CANDIDATE_PROFILE_PATH,
                             conn_factory=_chat_conn)
    return HTMLResponse("ok")


@app.post("/run/stop")
def run_stop():
    conn = _conn()
    actions.run_stop(conn)
    return HTMLResponse("ok")


@app.post("/pipeline/run-now")
async def pipeline_run_now():
    result = actions.pipeline_run_now(_conn(), _conn, DB_PATH, BRIEF_PATH, _background_tasks)
    if not result["ok"]:
        return HTMLResponse(f'<span class="denied">{escape(result["message"])}</span>')
    return HTMLResponse("ok")


@app.get("/pipeline/status", response_class=HTMLResponse)
def pipeline_status(request: Request):
    conn = _conn()
    return templates.TemplateResponse(
        request=request, name="_pipeline_status.html",
        context=context.pipeline_status_context(conn))


@app.post("/queue/{job_id}/skip")
async def queue_skip(job_id: int):
    conn = _conn()
    await actions.queue_skip(conn, job_id, brief_path=BRIEF_PATH,
                             candidate_profile_path=CANDIDATE_PROFILE_PATH,
                             conn_factory=_chat_conn)
    return HTMLResponse("ok")


@app.post("/queue/{job_id}/retry")
def queue_retry(job_id: int, confirm: bool = False):
    """?confirm=1 is the human confirming a held_unknown application was
    NOT submitted -- the only thing that releases that hold."""
    conn = _conn()
    return _span(actions.queue_retry(conn, job_id,
                                     confirm_not_submitted=confirm))


@app.post("/queue/{job_id}/priority")
def queue_priority(job_id: int, direction: str = Form(...)):
    conn = _conn()
    result = actions.queue_priority(conn, job_id, direction)
    if not result["ok"]:
        return _span(result)
    return HTMLResponse("ok")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    conn = _conn()
    return templates.TemplateResponse(
        request=request, name="settings.html",
        context={"active_nav": "settings",
                 **context.settings_context(conn, BRIEF_PATH,
                                            CANDIDATE_PROFILE_PATH)})


@app.post("/settings", response_class=HTMLResponse)
def settings_save(request: Request,
                  target_titles: str = Form(""),
                  title_families: str = Form(""),
                  search_locations: str = Form(""),
                  locations: str = Form(""),
                  work_authorization: str = Form(""),
                  excluded_companies: str = Form(""),
                  non_negotiables: str = Form(""),
                  remote_ok: str | None = Form(None),
                  salary_floor_inr: str = Form(""),
                  daily_cap: str = Form(""),
                  gate_threshold: str = Form(""),
                  staleness_days: str = Form(""),
                  scoring_model: str = Form(...),
                  max_score_per_run: str = Form(""),
                  brief_present: str | None = Form(None),
                  candidate_present: str | None = Form(None),
                  candidate_name: str = Form(""),
                  candidate_email: str = Form(""),
                  candidate_phone: str = Form(""),
                  linkedin_url: str = Form(""),
                  portfolio_url: str = Form("")):
    conn = _conn()
    form = {"target_titles": target_titles, "title_families": title_families,
            "search_locations": search_locations, "locations": locations,
            "work_authorization": work_authorization,
            "excluded_companies": excluded_companies,
            "non_negotiables": non_negotiables, "remote_ok": remote_ok,
            "salary_floor_inr": salary_floor_inr, "daily_cap": daily_cap,
            "gate_threshold": gate_threshold, "staleness_days": staleness_days,
            "scoring_model": scoring_model,
            "max_score_per_run": max_score_per_run,
            "brief_present": brief_present, "candidate_present": candidate_present,
            "candidate_name": candidate_name, "candidate_email": candidate_email,
            "candidate_phone": candidate_phone, "linkedin_url": linkedin_url,
            "portfolio_url": portfolio_url}
    result = actions.save_settings(conn, form, BRIEF_PATH, CANDIDATE_PROFILE_PATH)
    if not result["ok"]:
        return templates.TemplateResponse(
            request=request, name="settings.html",
            context={"active_nav": "settings",
                     **context.settings_context(
                         conn, BRIEF_PATH, CANDIDATE_PROFILE_PATH,
                         form=form, errors=result["errors"])})
    return templates.TemplateResponse(
        request=request, name="settings.html",
        context={"active_nav": "settings",
                 **context.settings_context(conn, BRIEF_PATH,
                                            CANDIDATE_PROFILE_PATH, saved=True)})
