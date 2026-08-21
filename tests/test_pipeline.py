import threading

import pytest

from career_agent import db
from career_agent.web import pipeline, worker


@pytest.fixture
def conn_factory(tmp_path):
    path = tmp_path / "t.db"
    c = db.connect(path)
    db.init_schema(c)

    def factory():
        return db.connect(path)
    return factory


async def test_run_background_sets_idle_and_logs_completion_on_success(
        conn_factory, monkeypatch, tmp_path):
    async def fake_run_once(args, progress=None):
        pass

    monkeypatch.setattr(pipeline.run_module, "run_once", fake_run_once)
    await pipeline.run_background(conn_factory, tmp_path / "t.db",
                                  tmp_path / "career_brief.toml")

    conn = conn_factory()
    state = worker.get_run_state(conn, "apply")  # sanity: apply row untouched
    assert state["status"] == "idle"
    pstate = worker.get_run_state(conn, "pipeline")
    assert pstate["status"] == "idle"
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "pipeline_completed" in types


async def test_run_background_sets_error_and_logs_on_failure(
        conn_factory, monkeypatch, tmp_path):
    async def boom(args, progress=None):
        raise RuntimeError("apify token missing")

    monkeypatch.setattr(pipeline.run_module, "run_once", boom)
    await pipeline.run_background(conn_factory, tmp_path / "t.db",
                                  tmp_path / "career_brief.toml")

    conn = conn_factory()
    pstate = worker.get_run_state(conn, "pipeline")
    assert pstate["status"] == "error"
    assert "apify token missing" in pstate["last_error"]
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "pipeline_error" in types


async def test_run_background_passes_paths_and_defaults_through(
        conn_factory, monkeypatch, tmp_path):
    seen = {}

    async def spy(args, progress=None):
        seen["db"] = args.db
        seen["brief"] = args.brief
        seen["boards"] = args.boards
        seen["max_score"] = args.max_score

    monkeypatch.setattr(pipeline.run_module, "run_once", spy)
    db_path = tmp_path / "t.db"
    brief_path = tmp_path / "career_brief.toml"
    await pipeline.run_background(conn_factory, db_path, brief_path)

    assert seen["db"] == str(db_path)
    assert seen["brief"] == str(brief_path)
    assert seen["boards"] == "ats_boards.toml"
    assert seen["max_score"] is None


async def test_run_background_runs_run_once_off_the_serving_event_loop(
        conn_factory, monkeypatch, tmp_path):
    """run_once is `async def` but its discovery phase never awaits: the
    Apify SDK's .call() blocks for minutes, 12-24 times a run. Awaiting it
    on the serving loop froze every route, including the dashboard's own
    3-second pipeline-status poll. It must run on a worker thread."""
    seen = {}

    async def record_thread(args, progress=None):
        seen["thread"] = threading.get_ident()

    monkeypatch.setattr(pipeline.run_module, "run_once", record_thread)
    await pipeline.run_background(conn_factory, tmp_path / "t.db",
                                  tmp_path / "career_brief.toml")

    assert seen["thread"] != threading.get_ident()


async def test_progress_is_written_to_the_pipeline_row(
        conn_factory, monkeypatch, tmp_path):
    async def fake_run_once(args, progress=None):
        progress(stage="score", found=40, scored=3, shortlisted=2)

    monkeypatch.setattr(pipeline.run_module, "run_once", fake_run_once)
    await pipeline.run_background(conn_factory, tmp_path / "t.db",
                                  tmp_path / "career_brief.toml")

    conn = conn_factory()
    row = worker.get_run_state(conn, "pipeline")
    assert row["stage"] == "score"
    assert row["found"] == 40
    assert row["scored"] == 3
    assert row["shortlisted"] == 2


async def test_progress_does_not_touch_the_apply_row(
        conn_factory, monkeypatch, tmp_path):
    async def fake_run_once(args, progress=None):
        progress(stage="score", found=40)

    monkeypatch.setattr(pipeline.run_module, "run_once", fake_run_once)
    await pipeline.run_background(conn_factory, tmp_path / "t.db",
                                  tmp_path / "career_brief.toml")

    conn = conn_factory()
    assert worker.get_run_state(conn, "apply")["stage"] is None
    assert worker.get_run_state(conn, "apply")["found"] == 0


async def test_a_message_becomes_an_activity_event(
        conn_factory, monkeypatch, tmp_path):
    async def fake_run_once(args, progress=None):
        progress(stage="clean", message="Discovery returned 40 listings.")

    monkeypatch.setattr(pipeline.run_module, "run_once", fake_run_once)
    await pipeline.run_background(conn_factory, tmp_path / "t.db",
                                  tmp_path / "career_brief.toml")

    conn = conn_factory()
    row = conn.execute(
        "SELECT type, payload FROM event WHERE type = 'pipeline_progress'"
    ).fetchone()
    assert row is not None
    assert row["payload"] == "Discovery returned 40 listings."
