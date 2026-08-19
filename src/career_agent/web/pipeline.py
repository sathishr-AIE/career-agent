from types import SimpleNamespace

from career_agent import run as run_module
from career_agent import store
from career_agent.web import worker


async def run_background(conn_factory, db_path, brief_path,
                         boards_path: str = "ats_boards.toml",
                         max_score: int = 25) -> None:
    """Runs the existing discover+hard-filter+score pipeline
    (run.run_once, unchanged) in the background, tracking progress in the
    'pipeline' row of run_state. The caller (the /pipeline/run-now
    endpoint) is responsible for flipping status to 'running' and logging
    'pipeline_started' synchronously before scheduling this — this
    function only handles the outcome, success or failure."""
    args = SimpleNamespace(db=str(db_path), brief=str(brief_path),
                           boards=boards_path, max_score=max_score)
    try:
        await run_module.run_once(args)
    except Exception as exc:
        conn = conn_factory()
        worker.set_run_state(conn, "pipeline", status="error", last_error=str(exc))
        store.log(conn, None, "pipeline_error", str(exc))
        return

    conn = conn_factory()
    worker.set_run_state(conn, "pipeline", status="idle", last_error=None)
    store.log(conn, None, "pipeline_completed")
