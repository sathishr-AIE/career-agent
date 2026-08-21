import asyncio
import datetime as dt
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from career_agent import db, outcomes, store
from career_agent.apply import ats as ats_apply
from career_agent.config import (MODEL_LABELS, SCORING_MODELS, CareerBrief,
                                 load_brief, save_brief)
from career_agent.web import overview, pipeline, worker

DB_PATH = Path("data/career.db")
BRIEF_PATH = Path("career_brief.toml")


# asyncio only holds a weak reference to a running task, so a fire-and-forget
# create_task can be garbage-collected mid-run. Tasks live here until done.
_background_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = _conn()
    # There is no pause/resume/stop for the pipeline by design, so a 'running'
    # row left by a process that died mid-run would strand the Run Now button
    # on a disabled "Running…" forever. A fresh process can only ever find it
    # that way after a crash or restart, so clear it.
    if worker.get_run_state(conn, "pipeline")["status"] == "running":
        worker.set_run_state(conn, "pipeline", status="error",
                             last_error="Interrupted by a server restart.")
    task = asyncio.create_task(worker.apply_worker_loop(_conn, BRIEF_PATH))
    yield
    task.cancel()


app = FastAPI(title="Career Agent", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

LIST_SQL = """
SELECT j.id, j.company, j.title, j.location, j.source, j.url,
       a.verdict, a.rationale, a.stage, a.weighted_score AS score,
       (SELECT ap.status FROM application ap
         WHERE ap.job_id = j.id
           AND ap.status IN ('in_flight','submitted','held_unknown','failed_permanent')
         ORDER BY ap.id DESC LIMIT 1) AS terminal_status,
       (SELECT ap.status FROM application ap WHERE ap.job_id = j.id
         ORDER BY ap.id DESC LIMIT 1) IS 'draft' AS has_draft
  FROM job j JOIN assessment a ON a.job_id = j.id
 WHERE j.merged_into_job_id IS NULL AND a.verdict IN ({placeholders})
 ORDER BY a.weighted_score DESC NULLS LAST, j.discovered_at DESC
"""


def _conn():
    conn = db.connect(DB_PATH)
    db.init_schema(conn)
    ats_apply.sweep_stale_in_flight(conn)
    return conn


def scheduled_task_installed(name: str = "CareerAgentDaily") -> bool:
    """No trigger is installed by default. The dashboard says so rather than
    letting you assume something ran overnight when nothing did."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-ScheduledTask -TaskName {name} -ErrorAction SilentlyContinue"],
            capture_output=True, text=True, timeout=10)
        return name in result.stdout
    except Exception:
        return False


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = _conn()
    brief = load_brief(BRIEF_PATH)
    today_submitted = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        " AND date(submitted_at) = date('now')").fetchone()["n"]
    kpi_data = overview.kpis(conn)
    return templates.TemplateResponse(
        request=request, name="overview.html",
        context={"active_nav": "dashboard", "brief": brief,
                 "daily_cap": brief.daily_cap,
                 "today_submitted": today_submitted,
                 "kpis": kpi_data,
                 "outcome_summary": overview.outcome_summary(conn),
                 "source_performance": overview.source_performance(conn),
                 "score_distribution": overview.score_distribution(conn),
                 "recent_discoveries": overview.recent_discoveries(conn),
                 "recent_outcomes": overview.recent_outcomes(conn),
                 "shortlisted_count": kpi_data["shortlisted"],
                 "pipeline_state": worker.get_run_state(conn, "pipeline")})


@app.get("/applications", response_class=HTMLResponse)
def applications(request: Request, show: str = "queue"):
    conn = _conn()
    verdicts = ["skip"] if show == "skipped" else ["submit", "hold"]
    sql = LIST_SQL.format(placeholders=",".join("?" * len(verdicts)))
    rows = conn.execute(sql, verdicts).fetchall()
    all_rows = conn.execute(
        LIST_SQL.format(placeholders="?,?,?"), ["submit", "hold", "skip"]
    ).fetchall()
    brief = load_brief(BRIEF_PATH)
    today_submitted = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        " AND date(submitted_at) = date('now')").fetchone()["n"]
    return templates.TemplateResponse(
        request=request, name="applications.html",
        context={"jobs": rows if show != "skipped" else all_rows,
                 "skipped_jobs": [r for r in all_rows if r["verdict"] == "skip"],
                 "show": show, "scheduled": scheduled_task_installed(),
                 "active_nav": "applications", "brief": brief,
                 "daily_cap": brief.daily_cap,
                 "today_submitted": today_submitted,
                 # first paint of the polled fragment, so the page ships the
                 # real status (and the mode toggle) instead of "Loading…"
                 **_run_status_context(conn)})


async def _do_apply(job_id: int, allow_skip: bool, event: str | None):
    conn = _conn()
    denial = worker.guard(conn, job_id, allow_skip=allow_skip, brief_path=BRIEF_PATH)
    if denial:
        return HTMLResponse(f'<span class="denied">{denial}</span>')

    if event:
        store.log(conn, job_id, event)

    try:
        result = await ats_apply.submit(conn, job_id, dry_run=True)
    except Exception as exc:
        return HTMLResponse(f'<span class="denied">{escape(str(exc))}</span>')
    if not result["ok"]:
        return HTMLResponse(f'<span class="denied">{escape(result["reason"])}</span>')
    return HTMLResponse('<span class="done">Applied</span>')


@app.post("/apply/{job_id}", response_class=HTMLResponse)
async def apply(job_id: int):
    return await _do_apply(job_id, allow_skip=False, event="human_applied")


@app.post("/override/{job_id}", response_class=HTMLResponse)
async def override(job_id: int):
    """Applying to something the gate skipped. The most valuable label the
    system produces, because it is the gate erring in the expensive direction."""
    return await _do_apply(job_id, allow_skip=True, event="human_override")


@app.post("/send/{job_id}", response_class=HTMLResponse)
async def send(job_id: int):
    conn = _conn()
    draft = conn.execute(
        "SELECT id FROM application WHERE job_id = ? AND status = 'draft'"
        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    if draft is None:
        return HTMLResponse(
            '<span class="denied">No draft to send yet. Click Apply first.</span>')

    denial = worker.guard(conn, job_id, allow_skip=True, brief_path=BRIEF_PATH)
    if denial:
        return HTMLResponse(f'<span class="denied">{denial}</span>')

    store.log(conn, job_id, "human_confirmed_send")

    try:
        result = await ats_apply.submit(conn, job_id, dry_run=False)
    except Exception as exc:
        return HTMLResponse(f'<span class="denied">{escape(str(exc))}</span>')
    if not result["ok"]:
        return HTMLResponse(f'<span class="denied">{escape(result["reason"])}</span>')
    # the run was parked on this draft awaiting review; unpark it, or the
    # worker loop returns early forever and the run never advances
    if worker.get_run_state(conn, "apply")["current_job_id"] == job_id:
        worker.set_run_state(conn, "apply", current_job_id=None)
    return HTMLResponse('<span class="done">Sent</span>')


@app.post("/dismiss/{job_id}", response_class=HTMLResponse)
def dismiss(job_id: int):
    conn = _conn()
    store.log(conn, job_id, "human_dismissed")
    return HTMLResponse('<span class="done">Dismissed</span>')


def _parse_date(raw: str, field: str, errors: dict) -> str | None:
    """Accept an ISO date, defaulting to today when blank. Parsed here, not
    declared as a typed Form parameter: a typed parameter makes FastAPI
    reject a bad value with a raw 422 before this handler runs, which skips
    the friendly error page entirely."""
    raw = (raw or "").strip()
    if not raw:
        return dt.date.today().isoformat()
    try:
        return dt.date.fromisoformat(raw).isoformat()
    except ValueError:
        errors[field] = "must be a date like 2026-08-20"
        return None


@app.post("/applied/{job_id}", response_class=HTMLResponse)
def mark_applied(job_id: int, when: str = Form("")):
    conn = _conn()
    errors: dict[str, str] = {}
    day = _parse_date(when, "when", errors)
    if errors:
        return HTMLResponse(
            f'<span class="denied">{escape(errors["when"])}</span>')

    try:
        store.mark_applied(conn, job_id, day)
    except sqlite3.IntegrityError:
        return HTMLResponse('<span class="denied">This job already has a'
                            ' live application.</span>')
    return HTMLResponse('<span class="done">Marked applied</span>')


@app.post("/outcome/{application_id}", response_class=HTMLResponse)
def record_outcome(application_id: int, type: str = Form(""),
                   occurred_at: str = Form(""), notes: str = Form("")):
    conn = _conn()
    row = conn.execute("SELECT status FROM application WHERE id = ?",
                       (application_id,)).fetchone()
    if row is None or row["status"] != "submitted":
        return HTMLResponse('<span class="denied">This application is not'
                            ' submitted yet. Mark the job applied before'
                            ' recording what came back.</span>')

    errors: dict[str, str] = {}
    day = _parse_date(occurred_at, "occurred_at", errors)
    if errors:
        return HTMLResponse(
            f'<span class="denied">{escape(errors["occurred_at"])}</span>')

    try:
        outcomes.record(conn, application_id, type, day,
                        notes=notes.strip() or None)
    except ValueError as exc:
        return HTMLResponse(f'<span class="denied">{escape(str(exc))}</span>')
    return HTMLResponse('<span class="done">Recorded</span>')


def _run_status_context(conn) -> dict:
    state = worker.get_run_state(conn, "apply")
    current_job = None
    if state["current_job_id"]:
        current_job = conn.execute(
            "SELECT j.id AS job_id, j.company, j.title FROM job j"
            " WHERE j.id = ?", (state["current_job_id"],)).fetchone()
    submitted = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
    ).fetchone()["n"]
    failed = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'failed_permanent'"
    ).fetchone()["n"]
    # jobs the gate skipped and nobody overrode (an override would have left
    # an application row behind), counted once via the latest assessment
    gate_skipped = conn.execute(
        "SELECT COUNT(*) n FROM job j JOIN assessment a ON a.job_id = j.id"
        " WHERE j.merged_into_job_id IS NULL AND a.verdict = 'skip'"
        "   AND a.id = (SELECT id FROM assessment a2 WHERE a2.job_id = j.id"
        "               ORDER BY a2.created_at DESC, a2.id DESC LIMIT 1)"
        "   AND NOT EXISTS (SELECT 1 FROM event e WHERE e.job_id = j.id"
        "                     AND e.type = 'human_override')"
        "   AND NOT EXISTS (SELECT 1 FROM application ap WHERE ap.job_id = j.id)"
    ).fetchone()["n"]
    stats = {
        "total_applied": submitted,
        "queued": worker.queue_count(conn),
        "in_progress": 1 if state["current_job_id"] else 0,
        "successful": submitted,
        "failed_skipped": failed + gate_skipped,
    }
    recent_events = conn.execute(
        "SELECT type, payload, occurred_at FROM event"
        " ORDER BY id DESC LIMIT 10").fetchall()
    return {"run_state": state, "current_job": current_job, "stats": stats,
            "recent_events": recent_events}


@app.get("/run/status", response_class=HTMLResponse)
def run_status(request: Request):
    conn = _conn()
    return templates.TemplateResponse(
        request=request, name="_run_status.html",
        context=_run_status_context(conn))


@app.post("/run/start")
async def run_start(mode: str = Form(...)):
    conn = _conn()
    worker.set_run_state(conn, "apply", status="running", mode=mode,
                         last_error=None)
    conn.execute("UPDATE run_state SET started_at = datetime('now')"
                 " WHERE kind = 'apply'")
    conn.commit()
    store.log(conn, None, "run_started", mode)
    await worker.apply_tick(conn, BRIEF_PATH)
    return HTMLResponse("ok")


@app.post("/run/pause")
def run_pause():
    conn = _conn()
    worker.set_run_state(conn, "apply", status="paused")
    store.log(conn, None, "run_paused")
    return HTMLResponse("ok")


@app.post("/run/resume")
async def run_resume():
    conn = _conn()
    worker.set_run_state(conn, "apply", status="running")
    store.log(conn, None, "run_resumed")
    await worker.apply_tick(conn, BRIEF_PATH)
    return HTMLResponse("ok")


@app.post("/run/stop")
def run_stop():
    conn = _conn()
    worker.set_run_state(conn, "apply", status="stopped", current_job_id=None)
    store.log(conn, None, "run_stopped")
    return HTMLResponse("ok")


@app.post("/pipeline/run-now")
async def pipeline_run_now():
    conn = _conn()
    state = worker.get_run_state(conn, "pipeline")
    if state["status"] not in ("idle", "error"):
        return HTMLResponse(
            '<span class="denied">A pipeline run is already in progress.</span>')
    worker.set_run_state(conn, "pipeline", status="running", last_error=None)
    store.log(conn, None, "pipeline_started")
    task = asyncio.create_task(
        pipeline.run_background(_conn, DB_PATH, BRIEF_PATH))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return HTMLResponse("ok")


@app.get("/pipeline/status", response_class=HTMLResponse)
def pipeline_status(request: Request):
    conn = _conn()
    return templates.TemplateResponse(
        request=request, name="_pipeline_status.html",
        context={"pipeline_state": worker.get_run_state(conn, "pipeline")})


@app.post("/queue/{job_id}/skip")
async def queue_skip(job_id: int):
    conn = _conn()
    store.log(conn, job_id, "job_skipped", "skipped by user")
    state = worker.get_run_state(conn, "apply")
    if state["current_job_id"] == job_id:
        worker.set_run_state(conn, "apply", current_job_id=None)
        nxt = worker.next_candidate(conn)
        if nxt is not None and nxt["job_id"] != job_id:
            await worker.apply_tick(conn, BRIEF_PATH)
    return HTMLResponse("ok")


@app.post("/queue/{job_id}/retry")
def queue_retry(job_id: int):
    conn = _conn()
    app_row = conn.execute(
        "SELECT status FROM application WHERE job_id = ?"
        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    if app_row is None or app_row["status"] != "failed":
        return HTMLResponse(
            '<span class="denied">Only a failed application can be'
            ' retried.</span>')
    lowest = conn.execute(
        "SELECT MIN(priority) p FROM job").fetchone()["p"]
    new_priority = (lowest - 1) if lowest is not None else 0
    conn.execute("UPDATE job SET priority = ? WHERE id = ?",
                 (new_priority, job_id))
    conn.commit()
    store.log(conn, job_id, "job_skipped", "retry requested; requeued")
    return HTMLResponse('<span class="done">Requeued</span>')


@app.post("/queue/{job_id}/priority")
def queue_priority(job_id: int, direction: str = Form(...)):
    conn = _conn()
    order = conn.execute(
        "SELECT j.id AS id, j.priority AS priority FROM job j"
        " JOIN assessment a ON a.job_id = j.id"
        f" WHERE {worker.QUEUE_WHERE}"
        " ORDER BY j.priority ASC NULLS LAST, j.id ASC").fetchall()
    ids = [r["id"] for r in order]
    if job_id not in ids:
        return HTMLResponse('<span class="denied">Not in the queue.</span>')
    pos = ids.index(job_id)
    neighbor_pos = pos - 1 if direction == "up" else pos + 1
    if not (0 <= neighbor_pos < len(ids)):
        return HTMLResponse("ok")  # already at the edge; nothing to swap
    for i, row in enumerate(order):
        conn.execute("UPDATE job SET priority = ? WHERE id = ?", (i, row["id"]))
    conn.commit()
    a, b = ids[pos], ids[neighbor_pos]
    pa = conn.execute("SELECT priority FROM job WHERE id = ?", (a,)).fetchone()["priority"]
    pb = conn.execute("SELECT priority FROM job WHERE id = ?", (b,)).fetchone()["priority"]
    conn.execute("UPDATE job SET priority = ? WHERE id = ?", (pb, a))
    conn.execute("UPDATE job SET priority = ? WHERE id = ?", (pa, b))
    conn.commit()
    return HTMLResponse("ok")


def _split_list(raw: str) -> list[str]:
    """List fields are comma-separated text inputs, which keeps the form
    static HTML with no JS array widgets."""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_int(raw: str, field: str, errors: dict) -> int | None:
    """Numeric fields are declared as str Form fields, exactly like
    salary_floor_inr, so a non-numeric value fails through our own 'Nothing
    was saved' page instead of FastAPI's automatic 422 -- which would bypass
    settings_save entirely and lose every other field the user typed.

    Their Form() defaults are "" for the same reason blankness is an error
    here: FastAPI substitutes a field's default for an empty submitted
    value, so a *value* default would turn "the user cleared the box" into a
    silent revert to that value, reported as "Settings saved"."""
    if not raw.strip():
        errors[field] = "is required"
        return None
    try:
        return int(raw)
    except ValueError:
        errors[field] = "must be a whole number"
        return None


def _validate_brief(target_titles, title_families, search_locations, locations,
                    work_authorization, excluded_companies, non_negotiables,
                    remote_ok, salary_floor_inr, daily_cap, gate_threshold,
                    staleness_days, errors) -> CareerBrief | None:
    """Build a CareerBrief from the form, recording any problems in `errors`.
    Validation is the pydantic model's job -- min_length on target_titles and
    search_locations, daily_cap >= 1, 0 <= gate_threshold <= 100,
    staleness_days >= 1 -- so no second rule set is written here."""
    try:
        return CareerBrief(
            target_titles=_split_list(target_titles),
            title_families=_split_list(title_families),
            search_locations=_split_list(search_locations),
            locations=_split_list(locations),
            work_authorization=_split_list(work_authorization),
            excluded_companies=_split_list(excluded_companies),
            non_negotiables=_split_list(non_negotiables),
            remote_ok=remote_ok is not None,
            salary_floor_inr=(int(salary_floor_inr)
                              if salary_floor_inr.strip() else None),
            daily_cap=daily_cap, gate_threshold=gate_threshold,
            staleness_days=staleness_days)
    except ValidationError as exc:
        for err in exc.errors():
            field = err["loc"][0] if err["loc"] else "form"
            errors[str(field)] = err["msg"]
    except ValueError:
        errors["salary_floor_inr"] = "must be a whole number, or blank"
    return None


def _settings_context(conn, *, form=None, errors=None, saved=False) -> dict:
    """Values shown come from the stores unless a failed submission is being
    re-rendered, in which case the user's own input is preserved."""
    brief = None
    brief_error = None
    try:
        brief = load_brief(BRIEF_PATH)
    except Exception as exc:
        # Falling back to CareerBrief() defaults would be a trap: saving
        # them would overwrite the file the user lost with a brief they
        # never chose. Disable that half of the form instead.
        brief_error = f"{BRIEF_PATH} could not be read: {exc}"
    settings = store.get_settings(conn)
    today_submitted = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        " AND date(submitted_at) = date('now')").fetchone()["n"]
    return {"active_nav": "settings", "brief": brief,
            "brief_error": brief_error,
            "daily_cap": brief.daily_cap if brief else "-",
            "today_submitted": today_submitted,
            "settings": settings, "scoring_models": SCORING_MODELS,
            "model_labels": MODEL_LABELS, "form": form or {},
            "errors": errors or {}, "saved": saved}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    conn = _conn()
    return templates.TemplateResponse(request=request, name="settings.html",
                                      context=_settings_context(conn))


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
                  brief_present: str | None = Form(None)):
    conn = _conn()
    form = {"target_titles": target_titles, "title_families": title_families,
            "search_locations": search_locations, "locations": locations,
            "work_authorization": work_authorization,
            "excluded_companies": excluded_companies,
            "non_negotiables": non_negotiables, "remote_ok": remote_ok,
            "salary_floor_inr": salary_floor_inr, "daily_cap": daily_cap,
            "gate_threshold": gate_threshold, "staleness_days": staleness_days,
            "scoring_model": scoring_model,
            "max_score_per_run": max_score_per_run}
    errors: dict[str, str] = {}

    # Every numeric field arrives as text and is parsed here, not declared
    # int on the route -- an int-typed Form field with no `required` on its
    # <input> lets FastAPI 422 the request before this function ever runs,
    # bypassing the "Nothing was saved" page and losing every other field
    # the user typed.
    #
    # max_score_per_run is an Agent Setting: its <input> is on the page
    # unconditionally, so it is always required.
    max_score_per_run_n = _parse_int(
        max_score_per_run, "max_score_per_run", errors)

    # The brief numerics are required only when brief_present says their
    # section was actually on the page. When the TOML could not be read that
    # section is hidden and this POST carries none of its fields, so
    # demanding three numbers here would break the "Agent Settings still
    # save" promise the banner on that page makes.
    daily_cap_n = gate_threshold_n = staleness_days_n = None
    if brief_present:
        daily_cap_n = _parse_int(daily_cap, "daily_cap", errors)
        gate_threshold_n = _parse_int(gate_threshold, "gate_threshold", errors)
        staleness_days_n = _parse_int(staleness_days, "staleness_days", errors)

    # Validate EVERYTHING before writing ANYTHING: a partial save would leave
    # the DB describing a state the TOML does not.
    brief = None
    if brief_present and None not in (daily_cap_n, gate_threshold_n,
                                      staleness_days_n):
        brief = _validate_brief(
            target_titles, title_families, search_locations, locations,
            work_authorization, excluded_companies, non_negotiables,
            remote_ok, salary_floor_inr, daily_cap_n, gate_threshold_n,
            staleness_days_n, errors)

    if scoring_model not in SCORING_MODELS:
        errors["scoring_model"] = f"unknown scoring model: {scoring_model}"
    if max_score_per_run_n is not None and max_score_per_run_n < 0:
        errors["max_score_per_run"] = "cannot be negative"

    if errors:
        return templates.TemplateResponse(
            request=request, name="settings.html",
            context=_settings_context(conn, form=form, errors=errors))

    if brief is not None:
        save_brief(BRIEF_PATH, brief)
    store.save_settings(conn, scoring_model, max_score_per_run_n)
    return templates.TemplateResponse(
        request=request, name="settings.html",
        context=_settings_context(conn, saved=True))
