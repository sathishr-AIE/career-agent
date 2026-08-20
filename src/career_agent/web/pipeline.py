import asyncio
from types import SimpleNamespace

from career_agent import run as run_module
from career_agent import store
from career_agent.web import worker


async def run_background(conn_factory, db_path, brief_path,
                         boards_path: str = "ats_boards.toml",
                         max_score: int | None = None) -> None:
    """Runs the existing discover+hard-filter+score pipeline
    (run.run_once, unchanged) in the background, tracking progress in the
    'pipeline' row of run_state. The caller (the /pipeline/run-now
    endpoint) is responsible for flipping status to 'running' and logging
    'pipeline_started' synchronously before scheduling this — this
    function only handles the outcome, success or failure."""
    args = SimpleNamespace(db=str(db_path), brief=str(brief_path),
                           boards=boards_path, max_score=max_score)
    try:
        # run_once is `async def` but its discovery phase is pure blocking
        # I/O with no await point (the Apify SDK's synchronous .call(), 12-24
        # times per run, plus sync httpx for ATS boards). Awaiting it here
        # would pin the serving event loop for minutes and freeze every
        # route, including this page's own status poller. asyncio.run inside
        # asyncio.to_thread gives it a fresh loop on a worker thread instead
        # -- and keeps run_once's own sqlite connection (opened inside it)
        # created and used on that one thread, as check_same_thread requires.
        await asyncio.to_thread(asyncio.run, run_module.run_once(args))
    except Exception as exc:
        conn = conn_factory()
        worker.set_run_state(conn, "pipeline", status="error", last_error=str(exc))
        store.log(conn, None, "pipeline_error", str(exc))
        return

    conn = conn_factory()
    worker.set_run_state(conn, "pipeline", status="idle", last_error=None)
    store.log(conn, None, "pipeline_completed")
