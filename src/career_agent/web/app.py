from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from career_agent import db, store
from career_agent.apply import ats as ats_apply
from career_agent.config import load_brief

DB_PATH = Path("data/career.db")
BRIEF_PATH = Path("career_brief.toml")

app = FastAPI(title="Career Agent")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

LIST_SQL = """
SELECT j.id, j.company, j.title, j.location, j.source, j.url,
       a.verdict, a.rationale, a.stage, a.weighted_score AS score,
       (SELECT COUNT(*) FROM application ap
         WHERE ap.job_id = j.id
           AND ap.status IN ('in_flight','submitted')) AS applied
  FROM job j JOIN assessment a ON a.job_id = j.id
 WHERE j.merged_into_job_id IS NULL AND a.verdict IN ({placeholders})
 ORDER BY a.weighted_score DESC NULLS LAST, j.discovered_at DESC
"""


def _conn():
    conn = db.connect(DB_PATH)
    db.init_schema(conn)
    ats_apply.sweep_stale_in_flight(conn)
    return conn


@app.get("/", response_class=HTMLResponse)
def index(request: Request, show: str = "queue"):
    conn = _conn()
    verdicts = ["skip"] if show == "skipped" else ["submit", "hold"]
    sql = LIST_SQL.format(placeholders=",".join("?" * len(verdicts)))
    rows = conn.execute(sql, verdicts).fetchall()
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"jobs": rows, "show": show})


def _guard(conn, job_id: int, allow_skip: bool) -> str | None:
    """Dashboard-side guardrail. The partial unique index is the real
    guarantee; this exists to produce a readable message."""
    a = conn.execute("SELECT verdict FROM assessment WHERE job_id = ?"
                     " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    if a is None:
        return "This job has not been scored yet."
    if a["verdict"] == "skip" and not allow_skip:
        return "The gate skipped this one. Use Apply anyway to override."

    brief = load_brief(BRIEF_PATH)
    used = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        " AND date(submitted_at) = date('now')").fetchone()["n"]
    if used >= brief.daily_cap:
        return f"Daily cap of {brief.daily_cap} reached."

    paused = conn.execute(
        "SELECT payload FROM event WHERE type='pause'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    if paused and paused["payload"] == "on":
        return "The agent is paused."
    return None


async def _do_apply(job_id: int, allow_skip: bool, event: str | None):
    conn = _conn()
    denial = _guard(conn, job_id, allow_skip)
    if denial:
        return HTMLResponse(f'<span class="denied">{denial}</span>')

    if event:
        store.log(conn, job_id, event)

    try:
        result = await ats_apply.submit(conn, job_id, dry_run=True)
    except Exception as exc:
        return HTMLResponse(f'<span class="denied">{exc}</span>')
    if not result["ok"]:
        return HTMLResponse(f'<span class="denied">{result["reason"]}</span>')
    return HTMLResponse('<span class="done">Applied</span>')


@app.post("/apply/{job_id}", response_class=HTMLResponse)
async def apply(job_id: int):
    return await _do_apply(job_id, allow_skip=False, event="human_applied")


@app.post("/override/{job_id}", response_class=HTMLResponse)
async def override(job_id: int):
    """Applying to something the gate skipped. The most valuable label the
    system produces, because it is the gate erring in the expensive direction."""
    return await _do_apply(job_id, allow_skip=True, event="human_override")


@app.post("/dismiss/{job_id}", response_class=HTMLResponse)
def dismiss(job_id: int):
    conn = _conn()
    store.log(conn, job_id, "human_dismissed")
    return HTMLResponse('<span class="done">Dismissed</span>')
