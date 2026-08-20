import threading

import pytest
from fastapi.testclient import TestClient

from career_agent import db, store
from career_agent.config import load_brief
from career_agent.web import app as web
from career_agent.web import pipeline
from career_agent.web import worker


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    db.init_schema(conn)
    conn.execute("INSERT INTO job (fingerprint, source, external_id, company,"
                 " company_normalized, title, title_normalized, url)"
                 " VALUES ('fp1','ats','1','Acme','acme','AI Engineer',"
                 " 'aiengineer','https://x/1')")
    conn.execute("INSERT INTO assessment (job_id, stage, role_fit, credibility,"
                 " opportunity, application_quality, eligibility_soft,"
                 " weighted_score, verdict, rationale, model, prompt_version)"
                 " VALUES (1,'scored',90,90,90,90,90,90,'submit',"
                 " 'strong match','m','gate-v1')")
    conn.execute("INSERT INTO job (fingerprint, source, external_id, company,"
                 " company_normalized, title, title_normalized, url)"
                 " VALUES ('fp2','ats','2','Globex','globex','ML Engineer',"
                 " 'engineerml','https://x/2')")
    conn.execute("INSERT INTO assessment (job_id, stage, role_fit, credibility,"
                 " opportunity, application_quality, eligibility_soft,"
                 " weighted_score, verdict, rationale, model, prompt_version)"
                 " VALUES (2,'scored',80,50,80,80,80,72,'skip',"
                 " 'credibility below floor','m','gate-v1')")
    conn.commit()
    monkeypatch.setattr(web, "DB_PATH", path)
    return TestClient(web.app)


@pytest.fixture
def running_pipeline(monkeypatch):
    """A run_once that keeps the pipeline in 'running' until the test ends.
    It has to be *releasable*: run_background hands run_once to
    asyncio.to_thread, whose worker thread is non-daemon, so a fake that
    never returns would hold the interpreter open at exit."""
    release = threading.Event()

    async def blocks(args):
        release.wait(10)

    monkeypatch.setattr(pipeline.run_module, "run_once", blocks)
    yield
    release.set()


def test_index_shows_submit_and_hold_with_rationale(client):
    r = client.get("/applications")
    assert "AI Engineer" in r.text
    assert "strong match" in r.text


def test_index_hides_skips_by_default(client):
    """The Skipped tab's markup is always present in the response (tabs are
    client-side CSS toggles, per the applications.html design), but the
    Queue tab itself must not list a skip-verdict job."""
    r = client.get("/applications")
    queue_html = r.text.split('id="tab-queue"')[1].split('id="tab-all"')[0]
    assert "ML Engineer" not in queue_html


def test_skipped_view_shows_them(client):
    r = client.get("/applications?show=skipped")
    assert "ML Engineer" in r.text
    assert "credibility below floor" in r.text


def test_dismiss_records_the_human_decision(client):
    r = client.post("/dismiss/1")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "human_dismissed" in types


def test_override_on_a_skip_records_the_override(client):
    r = client.post("/override/2")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "human_override" in types


def test_plain_apply_refuses_a_skip(client):
    r = client.post("/apply/2")
    assert "skip" in r.text.lower() or "override" in r.text.lower()


def test_submit_failure_message_is_escaped(client, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("<script>bad</script>")

    monkeypatch.setattr(web.ats_apply, "submit", boom)
    r = client.post("/override/2")
    assert r.status_code == 200
    assert "<script>" not in r.text


def test_apply_denial_reason_is_escaped(client, monkeypatch):
    async def denied(*args, **kwargs):
        return {"ok": False, "reason": "<script>bad</script>"}

    monkeypatch.setattr(web.ats_apply, "submit", denied)
    r = client.post("/apply/1")
    assert r.status_code == 200
    assert "<script>" not in r.text


def test_send_without_a_draft_is_refused(client, monkeypatch):
    calls = []

    async def spy(*args, **kwargs):
        calls.append(args)
        return {"ok": True}

    monkeypatch.setattr(web.ats_apply, "submit", spy)
    r = client.post("/send/1")
    assert calls == []
    assert "draft" in r.text.lower()


def test_send_after_apply_performs_a_real_submission(client, monkeypatch):
    calls = []

    async def fake_submit(conn, job_id, dry_run, filler=None):
        calls.append(dry_run)
        status = "draft" if dry_run else "submitted"
        conn.execute(
            "INSERT INTO application (job_id, resume_version, status)"
            " VALUES (?, 'v1', ?)", (job_id, status))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": status}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)

    client.post("/apply/1")
    r = client.post("/send/1")

    assert r.status_code == 200
    assert calls == [True, False]
    conn = db.connect(web.DB_PATH)
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "human_confirmed_send" in types


def test_index_offers_send_once_a_draft_exists(client):
    conn = db.connect(web.DB_PATH)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'v1', 'draft')")
    conn.commit()
    r = client.get("/applications")
    assert '/send/1' in r.text
    assert '/apply/1' not in r.text
    # and it leaves the Queue tab: a drafted job is in progress, not queued
    queue_html = r.text.split('id="tab-queue"')[1].split('id="tab-all"')[0]
    assert "AI Engineer" not in queue_html


def test_index_requeues_a_job_that_drafted_then_failed(client):
    """submit()'s real-send path never deletes the earlier draft row, so a
    job that drafted and then failed transiently carries both a 'draft' row
    and a later 'failed' row. has_draft must track the LATEST row (here,
    'failed'), not blanket row-membership -- else the job stays permanently
    hidden from the Queue tab and stuck showing a stale Send button, even
    though worker.next_candidate now treats it as retryable again."""
    conn = db.connect(web.DB_PATH)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'v1', 'draft')")
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'v1', 'failed')")
    conn.commit()
    r = client.get("/applications")
    # back in the Queue tab, like any other retryable job
    queue_html = r.text.split('id="tab-queue"')[1].split('id="tab-all"')[0]
    assert "AI Engineer" in queue_html
    # All Applications: Apply/Dismiss again, not a stale Send button
    all_html = r.text.split('id="tab-all"')[1].split('id="tab-skipped"')[0]
    assert '/apply/1' in all_html
    assert '/send/1' not in all_html


def test_index_shows_held_unknown_and_hides_apply_button(client):
    conn = db.connect(web.DB_PATH)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'v1', 'held_unknown')")
    conn.commit()
    r = client.get("/applications")
    assert "Held" in r.text
    assert "/apply/1" not in r.text


def test_index_shows_failed_permanent_and_hides_apply_button(client):
    conn = db.connect(web.DB_PATH)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'v1', 'failed_permanent')")
    conn.commit()
    r = client.get("/applications")
    assert "Failed permanently" in r.text
    assert "/apply/1" not in r.text


def test_scheduled_task_installed_is_false_for_a_missing_task():
    assert web.scheduled_task_installed("NoSuchCareerAgentTask") is False


def test_index_shows_no_schedule_banner_by_default(client):
    r = client.get("/applications")
    assert "No scheduled run is installed" in r.text


def test_index_hides_banner_when_a_schedule_is_installed(client, monkeypatch):
    monkeypatch.setattr(web, "scheduled_task_installed", lambda: True)
    r = client.get("/applications")
    assert "No scheduled run is installed" not in r.text


def test_run_start_sets_status_running_and_ticks_once(client, monkeypatch):
    async def fake_submit(conn, job_id, dry_run, filler=None):
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'draft')", (job_id,))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)
    r = client.post("/run/start", data={"mode": "manual"})
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "running"
    assert state["mode"] == "manual"
    assert state["current_job_id"] == 1  # job 1 is the higher-scored fixture row


def test_run_pause_sets_status_paused(client, monkeypatch):
    async def fake_submit(conn, job_id, dry_run, filler=None):
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'draft')", (job_id,))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)
    client.post("/run/start", data={"mode": "manual"})
    r = client.post("/run/pause")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    assert worker.get_run_state(conn, "apply")["status"] == "paused"


def test_run_resume_sets_status_running(client, monkeypatch):
    async def fake_submit(conn, job_id, dry_run, filler=None):
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'draft')", (job_id,))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)
    client.post("/run/start", data={"mode": "manual"})
    client.post("/run/pause")
    r = client.post("/run/resume")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    assert worker.get_run_state(conn, "apply")["status"] == "running"


def test_run_stop_clears_current_job(client, monkeypatch):
    async def fake_submit(conn, job_id, dry_run, filler=None):
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'draft')", (job_id,))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)
    client.post("/run/start", data={"mode": "manual"})
    r = client.post("/run/stop")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "stopped"
    assert state["current_job_id"] is None


def test_queue_skip_clears_current_job_and_logs(client, monkeypatch):
    async def fake_submit(conn, job_id, dry_run, filler=None):
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'draft')", (job_id,))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)
    client.post("/run/start", data={"mode": "manual"})
    r = client.post("/queue/1/skip")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    assert worker.get_run_state(conn, "apply")["current_job_id"] is None
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "job_skipped" in types


def test_queue_skip_advances_to_a_higher_ranked_candidate(client, monkeypatch):
    """Job 1 is stuck as 'current' (e.g. left over from before job 2's
    priority was bumped above it). Skipping job 1 must not just refuse to
    re-tick (that's the single-candidate case above) -- it must pick up the
    now-higher-ranked job 2, proving the fix's job-identity check advances
    the queue rather than freezing it whenever there IS somewhere to go."""
    conn = db.connect(web.DB_PATH)
    conn.execute("UPDATE assessment SET verdict = 'submit' WHERE job_id = 2")
    conn.execute("UPDATE job SET priority = 1 WHERE id = 1")
    conn.execute("UPDATE job SET priority = 0 WHERE id = 2")
    conn.commit()
    worker.set_run_state(conn, "apply", status="running", mode="manual",
                         current_job_id=1)

    async def fake_submit(conn, job_id, dry_run, filler=None):
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'draft')", (job_id,))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)
    r = client.post("/queue/1/skip")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    state = worker.get_run_state(conn, "apply")
    assert state["current_job_id"] == 2


def test_queue_retry_only_accepts_failed_status(client):
    conn = db.connect(web.DB_PATH)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'v1', 'submitted')")
    conn.commit()
    r = client.post("/queue/1/retry")
    assert r.status_code == 200
    assert "failed" in r.text.lower()


def test_queue_retry_on_a_failed_job_bumps_priority_to_front(client):
    conn = db.connect(web.DB_PATH)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'v1', 'failed')")
    conn.execute("UPDATE job SET priority = 5 WHERE id = 2")
    conn.commit()
    r = client.post("/queue/1/retry")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    row = conn.execute("SELECT priority FROM job WHERE id = 1").fetchone()
    assert row["priority"] < 5


def test_queue_priority_swaps_with_neighbor(client):
    conn = db.connect(web.DB_PATH)
    conn.execute("UPDATE assessment SET verdict = 'submit' WHERE job_id = 2")
    conn.execute("UPDATE job SET priority = 0 WHERE id = 1")
    conn.execute("UPDATE job SET priority = 1 WHERE id = 2")
    conn.commit()
    r = client.post("/queue/2/priority", data={"direction": "up"})
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    p1 = conn.execute("SELECT priority FROM job WHERE id = 1").fetchone()["priority"]
    p2 = conn.execute("SELECT priority FROM job WHERE id = 2").fetchone()["priority"]
    assert p2 < p1


def test_queue_priority_ignores_jobs_that_are_not_in_the_queue(client):
    """Job 2's verdict is 'skip', so it isn't in the Queue tab at all.
    Nudging job 1 down must be a no-op rather than a silent swap against a
    row the user cannot see -- which is what made the buttons look dead."""
    conn = db.connect(web.DB_PATH)
    conn.execute("UPDATE job SET priority = 0 WHERE id = 1")
    conn.execute("UPDATE job SET priority = 1 WHERE id = 2")
    conn.commit()
    r = client.post("/queue/1/priority", data={"direction": "down"})
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    p1 = conn.execute("SELECT priority FROM job WHERE id = 1").fetchone()["priority"]
    p2 = conn.execute("SELECT priority FROM job WHERE id = 2").fetchone()["priority"]
    assert p1 < p2  # unchanged: the skipped job was never a neighbor


def test_queue_priority_refuses_a_job_that_is_not_a_candidate(client):
    conn = db.connect(web.DB_PATH)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'v1', 'draft')")
    conn.commit()
    r = client.post("/queue/1/priority", data={"direction": "up"})
    assert "not in the queue" in r.text.lower()


def test_send_clears_the_run_state_so_the_worker_can_advance(client, monkeypatch):
    """Manual mode parks the run on a draft. If /send doesn't release
    current_job_id, apply_tick returns early forever and the run is dead."""
    async def fake_submit(conn, job_id, dry_run, filler=None):
        status = "draft" if dry_run else "submitted"
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', ?)", (job_id, status))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": status}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)
    client.post("/run/start", data={"mode": "manual"})
    conn = db.connect(web.DB_PATH)
    assert worker.get_run_state(conn, "apply")["current_job_id"] == 1

    r = client.post("/send/1")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    assert worker.get_run_state(conn, "apply")["current_job_id"] is None


def test_run_start_clears_a_stale_last_error(client, monkeypatch):
    async def fake_submit(conn, job_id, dry_run, filler=None):
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'draft')", (job_id,))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)
    conn = db.connect(web.DB_PATH)
    worker.set_run_state(conn, "apply", status="error",
                         last_error="browser crashed")
    client.post("/run/start", data={"mode": "manual"})
    conn = db.connect(web.DB_PATH)
    assert worker.get_run_state(conn, "apply")["last_error"] is None


def _mode_toggle(html: str) -> str:
    return html.split('class="mode-toggle"')[1].split("</span>")[0]


def test_applications_page_offers_a_mode_toggle(client):
    toggle = _mode_toggle(client.get("/applications").text)
    assert 'name="mode"' in toggle
    assert 'value="auto"' in toggle
    assert 'value="manual"' in toggle
    # and Start sends whatever the toggle holds
    assert "hx-include=\"[name='mode']\"" in client.get("/run/status").text


def test_mode_toggle_is_disabled_while_a_run_is_running(client, monkeypatch):
    async def fake_submit(conn, job_id, dry_run, filler=None):
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'draft')", (job_id,))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)
    assert "disabled" not in _mode_toggle(client.get("/applications").text)
    client.post("/run/start", data={"mode": "manual"})
    assert "disabled" in _mode_toggle(client.get("/applications").text)


@pytest.mark.parametrize("mode", ["auto", "manual"])
def test_run_start_records_the_mode_it_was_given(client, monkeypatch, mode):
    async def fake_submit(conn, job_id, dry_run, filler=None):
        status = "draft" if dry_run else "submitted"
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', ?)", (job_id, status))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": status}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)
    client.post("/run/start", data={"mode": mode})
    conn = db.connect(web.DB_PATH)
    assert worker.get_run_state(conn, "apply")["mode"] == mode


def test_run_status_shows_idle_start_button(client):
    r = client.get("/run/status")
    assert r.status_code == 200
    assert "Start" in r.text
    assert 'hx-post="/run/start"' in r.text


def test_run_status_shows_pause_button_and_current_job_when_running(client, monkeypatch):
    async def fake_submit(conn, job_id, dry_run, filler=None):
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'draft')", (job_id,))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)
    client.post("/run/start", data={"mode": "manual"})
    r = client.get("/run/status")
    assert "Pause" in r.text
    assert "AI Engineer" in r.text  # current job's title, from the fixture


def test_applications_page_has_nav_and_tabs(client):
    r = client.get("/applications")
    assert 'href="/"' in r.text  # Dashboard nav link
    assert 'href="/applications"' in r.text
    assert "Queue" in r.text and "Skipped" in r.text


def test_applications_page_embeds_run_status_polling(client):
    r = client.get("/applications")
    assert 'hx-get="/run/status"' in r.text
    assert "every 2s" in r.text


def test_applications_page_shows_career_brief_panel(client):
    r = client.get("/applications")
    assert "Career Brief" in r.text


def test_failed_skipped_counts_gate_skips_until_they_are_overridden(client, monkeypatch):
    """Job 2 is a skip nobody acted on -- 'Failed/Skipped' should say so
    instead of reporting 0 until an application actually fails."""
    conn = db.connect(web.DB_PATH)
    stats = web._run_status_context(conn)["stats"]
    assert stats["failed_skipped"] == 1
    assert stats["total_applied"] == stats["successful"] == 0

    async def fake_submit(conn, job_id, dry_run, filler=None):
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'draft')", (job_id,))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)
    client.post("/override/2")
    conn = db.connect(web.DB_PATH)
    assert web._run_status_context(conn)["stats"]["failed_skipped"] == 0


def test_run_status_shows_stats(client, monkeypatch):
    async def fake_submit(conn, job_id, dry_run, filler=None):
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'submitted')", (job_id,))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "submitted"}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)
    client.post("/run/start", data={"mode": "auto"})
    r = client.get("/run/status")
    assert r.status_code == 200
    assert "Total Applied" in r.text


def test_pipeline_run_now_flips_status_and_logs_synchronously(client, running_pipeline):
    r = client.post("/pipeline/run-now")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    assert worker.get_run_state(conn, "pipeline")["status"] == "running"
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "pipeline_started" in types


def test_pipeline_run_now_refuses_a_second_concurrent_run(client, running_pipeline):
    client.post("/pipeline/run-now")
    r = client.post("/pipeline/run-now")
    assert "already" in r.text.lower()


def test_pipeline_status_shows_run_now_button_when_idle(client):
    r = client.get("/pipeline/status")
    assert r.status_code == 200
    assert 'hx-post="/pipeline/run-now"' in r.text
    assert "Run Now" in r.text


def test_pipeline_status_shows_running_state(client, running_pipeline):
    client.post("/pipeline/run-now")
    r = client.get("/pipeline/status")
    assert "Running" in r.text


def test_startup_clears_a_pipeline_run_stranded_by_a_crash(client):
    """A process that dies mid-run leaves run_state stuck at 'running', and
    there are no pause/resume/stop endpoints for the pipeline -- the Run Now
    button stays a disabled "Running…" forever. Startup is the only place
    that can tell the difference, so it clears it."""
    conn = db.connect(web.DB_PATH)
    worker.set_run_state(conn, "pipeline", status="running", last_error=None)

    with TestClient(web.app):  # entering the context manager runs lifespan
        pass

    conn = db.connect(web.DB_PATH)
    state = worker.get_run_state(conn, "pipeline")
    assert state["status"] == "error"
    assert "restart" in state["last_error"].lower()
    # and the page offers Run Now again rather than a dead disabled button
    assert 'hx-post="/pipeline/run-now"' in client.get("/pipeline/status").text


def test_root_renders_overview_not_a_redirect(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "Dashboard" in r.text


def test_overview_shows_kpi_cards(client):
    r = client.get("/")
    assert "Discovered" in r.text
    assert "Shortlisted" in r.text


def test_overview_shows_recent_discoveries_from_fixture(client):
    r = client.get("/")
    assert "AI Engineer" in r.text  # job 1 from the client fixture
    assert "Acme" in r.text


def test_overview_ready_to_apply_cta_links_to_applications(client):
    r = client.get("/")
    assert 'href="/applications"' in r.text


def test_overview_embeds_pipeline_status_polling(client):
    r = client.get("/")
    assert 'hx-get="/pipeline/status"' in r.text
    # must be innerHTML, not outerHTML — outerHTML replaces the polling
    # element itself and htmx never re-fires the poll after that (this
    # exact regression happened once already, in the sibling plan's
    # /run/status poller)
    assert 'hx-swap="innerHTML"' in r.text


BRIEF_TOML = """\
# Keep this comment.
target_titles = ["AI Engineer"]
search_locations = ["Chennai"]
locations = ["Chennai", "Remote"]
remote_ok = true
salary_floor_inr = 1200000
daily_cap = 5
gate_threshold = 72
staleness_days = 30
"""


@pytest.fixture
def brief_path(tmp_path, monkeypatch):
    p = tmp_path / "career_brief.toml"
    p.write_text(BRIEF_TOML, encoding="utf-8")
    monkeypatch.setattr(web, "BRIEF_PATH", p)
    return p


def _form(**overrides):
    # brief_present mirrors the hidden input the rendered Career Brief
    # section always carries when the brief loaded (see brief_path fixture),
    # so this models a real submission of that section, not one with it
    # hidden.
    base = {"target_titles": "AI Engineer", "title_families": "",
            "search_locations": "Chennai", "locations": "Chennai, Remote",
            "work_authorization": "", "excluded_companies": "",
            "non_negotiables": "", "remote_ok": "on",
            "salary_floor_inr": "1200000", "daily_cap": "5",
            "gate_threshold": "72", "staleness_days": "30",
            "scoring_model": "claude-sonnet-5", "max_score_per_run": "25",
            "brief_present": "1"}
    return {**base, **overrides}


def test_settings_page_renders_current_values(client, brief_path):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Career Brief" in r.text
    assert "Agent Settings" in r.text
    assert "claude-sonnet-5" in r.text
    assert "AI Engineer" in r.text


def test_settings_nav_link_is_active_on_the_settings_page(client, brief_path):
    """base.html emits href="/settings" in the sidebar of EVERY page, so the
    bare link proves nothing about which page rendered. Only the active
    highlight, which comes from active_nav, does."""
    assert '<a href="/settings" class="active">' in client.get("/settings").text
    # ...and it is not lit on a page that isn't Settings
    assert '<a href="/settings" class="active">' not in client.get("/").text


def test_saving_persists_the_agent_settings(client, brief_path):
    r = client.post("/settings", data=_form(
        scoring_model="claude-haiku-4-5", max_score_per_run="40"))
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    s = store.get_settings(conn)
    assert s["scoring_model"] == "claude-haiku-4-5"
    assert s["max_score_per_run"] == 40


def test_saving_writes_the_brief_and_keeps_its_comments(client, brief_path):
    client.post("/settings", data=_form(daily_cap="9",
                                        target_titles="AI Engineer, ML Engineer"))
    text = brief_path.read_text(encoding="utf-8")
    assert "# Keep this comment." in text
    brief = load_brief(brief_path)
    assert brief.daily_cap == 9
    assert brief.target_titles == ["AI Engineer", "ML Engineer"]


def test_an_invalid_brief_leaves_both_stores_untouched(client, brief_path):
    before = brief_path.read_text(encoding="utf-8")
    r = client.post("/settings", data=_form(
        target_titles="",                      # violates min_length=1
        scoring_model="claude-haiku-4-5"))
    assert r.status_code == 200
    assert "target_titles" in r.text
    assert brief_path.read_text(encoding="utf-8") == before
    conn = db.connect(web.DB_PATH)
    assert store.get_settings(conn)["scoring_model"] == "claude-sonnet-5"


def test_an_unknown_model_is_rejected(client, brief_path):
    before = brief_path.read_text(encoding="utf-8")
    r = client.post("/settings", data=_form(scoring_model="gpt-4"))
    assert "scoring_model" in r.text
    assert brief_path.read_text(encoding="utf-8") == before


def test_a_missing_brief_file_does_not_crash_the_page(client, tmp_path,
                                                      monkeypatch):
    """Rendering brief defaults would be a trap: saving them would then
    write a brief the user never chose over the file they lost."""
    monkeypatch.setattr(web, "BRIEF_PATH", tmp_path / "gone.toml")
    r = client.get("/settings")
    assert r.status_code == 200
    assert "could not be read" in r.text
    # the Agent Settings half still works, since it does not need the file
    assert "Scoring model" in r.text


def test_a_non_numeric_daily_cap_is_rejected_without_a_422(client, brief_path):
    """daily_cap is declared as a str Form field and parsed by hand, exactly
    like salary_floor_inr already was -- an int-typed Form field with no
    `required` on its <input> would let FastAPI 422 the request before
    settings_save ever runs, skipping the friendly error page entirely. A
    non-numeric value is what used to 422 under the old int-typed
    declaration, so that's what this exercises; the blank case, which is a
    different bug, is covered by the test below."""
    before = brief_path.read_text(encoding="utf-8")
    r = client.post("/settings", data=_form(daily_cap="lots"))
    assert r.status_code == 200
    assert "daily_cap" in r.text
    assert brief_path.read_text(encoding="utf-8") == before
    conn = db.connect(web.DB_PATH)
    assert store.get_settings(conn)["scoring_model"] == "claude-sonnet-5"


def test_agent_settings_still_save_when_the_brief_is_missing(client, tmp_path,
                                                              monkeypatch):
    """The whole point of brief_present: when the TOML can't be read the
    brief section is hidden, so a real submission from that page carries no
    brief fields and no brief_present marker at all -- and Agent Settings
    must still save rather than being rejected for a blank brief."""
    monkeypatch.setattr(web, "BRIEF_PATH", tmp_path / "gone.toml")
    r = client.post("/settings", data={
        "scoring_model": "claude-haiku-4-5",
        "max_score_per_run": "40"})
    assert r.status_code == 200
    assert "Settings saved" in r.text
    assert "Nothing was saved" not in r.text
    conn = db.connect(web.DB_PATH)
    s = store.get_settings(conn)
    assert s["scoring_model"] == "claude-haiku-4-5"
    assert s["max_score_per_run"] == 40


def test_a_blank_numeric_is_an_error_not_a_silent_revert(client, brief_path):
    """The nastiest shape of the Form()-default trap. FastAPI substitutes a
    field's default for an EMPTY submitted value, so while these fields
    defaulted to "5"/"72"/"30"/"25", clearing the daily cap box and saving
    reported "Settings saved" while quietly reverting four values the user
    never chose -- rewriting the version-controlled TOML and, via
    gate_threshold, changing which jobs get submitted. The defaults are ""
    now, so a cleared box is a field error and nothing is written."""
    before = brief_path.read_text(encoding="utf-8")
    settings_before = dict(store.get_settings(db.connect(web.DB_PATH)))

    r = client.post("/settings", data=_form(
        daily_cap="", gate_threshold="80", staleness_days="45",
        max_score_per_run="40", scoring_model="claude-haiku-4-5"))

    assert r.status_code == 200
    assert "Nothing was saved" in r.text
    assert "Settings saved" not in r.text
    assert "daily_cap" in r.text
    # all-or-nothing: neither store moved, and the TOML is byte-identical
    assert brief_path.read_text(encoding="utf-8") == before
    assert dict(store.get_settings(db.connect(web.DB_PATH))) == settings_before


def test_a_blank_max_score_per_run_is_an_error_too(client, brief_path):
    """max_score_per_run is an Agent Setting, so unlike the brief numerics
    it is required on every POST, brief_present or not."""
    r = client.post("/settings", data=_form(max_score_per_run=""))
    assert "Nothing was saved" in r.text
    assert "max_score_per_run" in r.text
