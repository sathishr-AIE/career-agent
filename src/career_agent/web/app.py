import asyncio
import subprocess
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from career_agent import db, store
from career_agent.apply import ats as ats_apply
from career_agent.web import worker

DB_PATH = Path("data/career.db")
BRIEF_PATH = Path("career_brief.toml")


@asynccontextmanager
async def lifespan(app: FastAPI):
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
       (SELECT COUNT(*) FROM application ap
         WHERE ap.job_id = j.id AND ap.status = 'draft') AS has_draft
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
def index(request: Request, show: str = "queue"):
    conn = _conn()
    verdicts = ["skip"] if show == "skipped" else ["submit", "hold"]
    sql = LIST_SQL.format(placeholders=",".join("?" * len(verdicts)))
    rows = conn.execute(sql, verdicts).fetchall()
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"jobs": rows, "show": show,
                 "scheduled": scheduled_task_installed()})


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
    return HTMLResponse('<span class="done">Sent</span>')


@app.post("/dismiss/{job_id}", response_class=HTMLResponse)
def dismiss(job_id: int):
    conn = _conn()
    store.log(conn, job_id, "human_dismissed")
    return HTMLResponse('<span class="done">Dismissed</span>')


def _run_status_context(conn) -> dict:
    state = worker.get_run_state(conn, "apply")
    current_job = None
    if state["current_job_id"]:
        current_job = conn.execute(
            "SELECT j.id AS job_id, j.company, j.title FROM job j"
            " WHERE j.id = ?", (state["current_job_id"],)).fetchone()
    stats = {
        "total_applied": conn.execute(
            "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        ).fetchone()["n"],
        "queued": worker.queue_count(conn),
        "in_progress": 1 if state["current_job_id"] else 0,
        "successful": conn.execute(
            "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        ).fetchone()["n"],
        "failed_skipped": conn.execute(
            "SELECT COUNT(*) n FROM application WHERE status = 'failed_permanent'"
        ).fetchone()["n"],
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
    worker.set_run_state(conn, "apply", status="running", mode=mode)
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
        "SELECT id, priority FROM job WHERE merged_into_job_id IS NULL"
        " ORDER BY priority ASC NULLS LAST, id ASC").fetchall()
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
