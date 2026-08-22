import datetime as dt
import threading
import time
from pathlib import Path

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

    async def blocks(args, progress=None):
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


def test_index_offers_mark_applied_when_a_draft_exists(client):
    """The All Applications tab has no Send/Apply UI (Task 4 removed it): a
    job with only a draft application row has no submitted row, so it is
    untracked and gets the same Mark applied control as any other job."""
    conn = db.connect(web.DB_PATH)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'v1', 'draft')")
    conn.commit()
    r = client.get("/applications")
    assert '/send/1' not in r.text
    assert 'hx-post="/applied/1"' in r.text
    # and it leaves the Queue tab: a drafted job is in progress, not queued
    queue_html = r.text.split('id="tab-queue"')[1].split('id="tab-all"')[0]
    assert "AI Engineer" not in queue_html


def test_index_requeues_a_job_that_drafted_then_failed(client):
    """submit()'s real-send path never deletes the earlier draft row, so a
    job that drafted and then failed transiently carries both a 'draft' row
    and a later 'failed' row. has_draft must track the LATEST row (here,
    'failed'), not blanket row-membership -- else the job stays permanently
    hidden from the Queue tab, even though worker.next_candidate now treats
    it as retryable again."""
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
    # All Applications: Mark applied/Dismiss again, not a stale Send button
    all_html = r.text.split('id="tab-all"')[1].split('id="tab-skipped"')[0]
    assert 'hx-post="/applied/1"' in all_html
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


def test_pipeline_status_renders_the_stage_track(client):
    r = client.get("/pipeline/status")
    for label in ("Discover", "Clean", "Filter", "Score", "Ready"):
        assert label in r.text


def test_pipeline_status_renders_the_counters(client):
    conn = db.connect(web.DB_PATH)
    worker.set_run_state(conn, "pipeline", stage="score", found=91,
                         duplicates=7, passed=27, scored=14, shortlisted=6)
    r = client.get("/pipeline/status")
    assert "Jobs Found" in r.text and "91" in r.text
    assert "Passed Filter" in r.text and "27" in r.text
    assert "Shortlisted" in r.text and "6" in r.text


def test_pipeline_status_renders_the_activity_feed(client):
    conn = db.connect(web.DB_PATH)
    # The feed is scoped to the current run (started_at or later) so a
    # freshly started run never shows a previous run's lines -- see
    # test_the_activity_feed_is_scoped_to_the_current_run below. That scoping
    # needs a real started_at for this event to be in range.
    worker.set_run_state(conn, "pipeline", status="running")
    conn.execute("UPDATE run_state SET started_at = datetime('now', '-1 minute')"
                 " WHERE kind = 'pipeline'")
    conn.execute("INSERT INTO event (job_id, type, payload)"
                 " VALUES (NULL, 'pipeline_progress', 'Discovery returned 40.')")
    conn.commit()
    r = client.get("/pipeline/status")
    assert "Discovery returned 40." in r.text


def test_the_no_auto_apply_note_is_present(client):
    r = client.get("/pipeline/status")
    assert "No automatic applications" in r.text


def test_progress_bar_interpolates_within_the_score_stage(client):
    """Review fix: pct used to jump straight to the end of the score band
    ((idx+1)*100//5 == 80) and sit there for the run's longest phase. It must
    now move as `scored` climbs toward max_score_per_run (25, the fixture's
    default setting), not just jump between five fixed positions."""
    conn = db.connect(web.DB_PATH)
    worker.set_run_state(conn, "pipeline", stage="score", scored=0)
    start = client.get("/pipeline/status").text
    start_pct = int(start.split('style="width:')[1].split('%')[0])

    worker.set_run_state(conn, "pipeline", stage="score", scored=20)
    later = client.get("/pipeline/status").text
    later_pct = int(later.split('style="width:')[1].split('%')[0])

    assert start_pct == 60   # band start: 3 whole stages done (3*20)
    assert later_pct == 76   # 60 + (20 * 20 // 25)


def test_the_activity_feed_is_scoped_to_the_current_run(client):
    """Review fix: the feed query had no run scoping, so opening the monitor
    on a freshly started run showed the *previous* run's twelve lines next to
    all-zero counters. Also locks in newest-first DOM order, which .activity's
    flex-direction:column-reverse (base.html) depends on to stay anchored to
    the newest line without JS re-scrolling on every 3s poll."""
    conn = db.connect(web.DB_PATH)
    conn.execute("INSERT INTO event (job_id, type, payload, occurred_at)"
                 " VALUES (NULL, 'pipeline_progress', 'stale from last run',"
                 " datetime('now', '-1 hour'))")
    conn.commit()

    worker.set_run_state(conn, "pipeline", status="running", stage="discover")
    conn.execute("UPDATE run_state SET started_at = datetime('now', '-1 minute')"
                 " WHERE kind = 'pipeline'")
    conn.execute("INSERT INTO event (job_id, type, payload)"
                 " VALUES (NULL, 'pipeline_progress', 'first message')")
    conn.execute("INSERT INTO event (job_id, type, payload)"
                 " VALUES (NULL, 'pipeline_progress', 'second message')")
    conn.commit()

    r = client.get("/pipeline/status")
    assert "stale from last run" not in r.text
    assert r.text.index("second message") < r.text.index("first message")


def test_run_now_discards_the_response_instead_of_blanking_the_monitor(client):
    """Review fix: the button used to hx-target="#pipeline-status"
    hx-swap="outerHTML" the bare string "ok" over the div this task widened
    from a two-element strip into the entire monitor -- nuking the
    track/counters/feed/note for up to 3s on every click. hx-swap="none"
    discards the response; the poller's own independent 3s tick picks up the
    new state instead."""
    button = client.get("/pipeline/status").text.split("Run Now")[0]
    assert 'hx-swap="none"' in button
    assert "outerHTML" not in button


def test_the_run_now_button_is_reachable_in_a_rendered_page(client):
    """The Critical review finding: `.live-overlay{display:none}` used to
    hide everything inside #runOverlay unconditionally, including the
    idle-state Run Now button -- the only element that can ever add `.open`.
    Every path to opening the modal was unreachable.

    No other test in this suite can catch that class of bug: TestClient only
    ever inspects HTML text, never the CSS that decides what is actually
    visible. This is the one check that renders the real page and looks at
    computed visibility, via a headless, offline (all network blocked)
    Playwright browser -- already a hard dependency of this project
    (career_agent.apply.ats) -- against the exact HTML GET / returns, no
    live server needed. Skips itself if a browser isn't installed rather
    than failing the suite in an environment that lacks one."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        pytest.skip(f"playwright not importable: {exc}")

    html = client.get("/").text

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"no browser available for playwright: {exc}")

        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.abort())  # fully offline
            page.set_content(html)

            run_now = page.locator("#pipeline-status .btn.primary")
            assert run_now.is_visible(), (
                "Run Now must be visible in the closed (idle) state -- "
                "it is the only path to opening the modal")
            assert not page.locator(".live-head").is_visible(), (
                "modal-only chrome must stay hidden while closed")
            assert not page.locator("#monitor-body").is_visible()

            run_now.click()

            overlay_class = (
                page.locator("#runOverlay").get_attribute("class") or "")
            assert "open" in overlay_class.split(), "click must open the modal"
            assert page.locator(".live-head").is_visible(), (
                "modal chrome must appear once open")
            assert page.locator("#monitor-body").is_visible()
        finally:
            browser.close()


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


def test_the_modal_shell_is_outside_the_poller(client):
    """Open/closed is client state and the poller replaces its target every
    3s. Inside the polled region, every swap would slam the modal shut or
    reopen one the user closed -- the bug already fixed once for the
    Auto/Manual toggle."""
    r = client.get("/")
    before = r.text.split('id="pipeline-status-poller"')[0]
    assert 'id="runOverlay"' in before, "modal shell precedes the poller"


def test_overview_page_render_shows_the_pipeline_stage_track(client):
    """Review fix: overview.html includes _pipeline_status.html (which reads
    a route-supplied `stages` -- the 5-item pipeline stage list) and, further
    down the *same* template, had its own unrelated `{% set stages = [...] %}`
    for the KPI funnel. The include only resolved to the route-supplied value
    because the funnel's {% set %} sat below it in the template; reordering
    them would have silently fed the funnel's (label, count) pairs into the
    stage track instead. Every other stage-track test (e.g.
    test_pipeline_status_renders_the_stage_track above) hits /pipeline/status
    directly, whose fragment render never sees the parent template's
    variables at all -- so none of them could ever have caught this. Only a
    full render of / can."""
    r = client.get("/")
    for label in ("Discover", "Clean", "Filter", "Score", "Ready"):
        assert f'<div class="stage-label">{label}</div>' in r.text


def test_still_running_toast_clears_when_the_run_finishes_or_reopened(client):
    """Review fix: closeRunMonitor() added #runToast's `open` class when the
    modal was dismissed mid-run, but nothing ever removed it -- not when the
    run finished, not when the modal was reopened. Live-reproduced: after the
    poller reports data-status="idle", the toast kept saying "Career Agent is
    still running..." forever, right next to an Idle pill and a Run Now
    button that both correctly showed the run was over -- recreating, in the
    toast, the exact "can't tell progress from a hang" problem this whole
    plan exists to fix."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        pytest.skip(f"playwright not importable: {exc}")

    conn = db.connect(web.DB_PATH)
    worker.set_run_state(conn, "pipeline", status="running")
    conn.execute("UPDATE run_state SET started_at = datetime('now')"
                 " WHERE kind = 'pipeline'")
    conn.commit()
    html = client.get("/").text

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"no browser available for playwright: {exc}")

        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.abort())  # fully offline
            page.set_content(html)

            def toast_open():
                cls = page.locator("#runToast").get_attribute("class") or ""
                return "open" in cls.split()

            # Dismiss while a run is going -> the toast appears.
            page.evaluate("closeRunMonitor()")
            assert toast_open(), "dismissing mid-run must show the toast"

            # The run finishes. In reality the poller's next swap would set
            # this; set it directly to isolate the setInterval logic itself
            # from the (already-covered-elsewhere) polling mechanics.
            page.evaluate(
                "document.getElementById('monitor-body').dataset.status = 'idle'")

            # Nobody re-opens or re-dismisses the modal here -- only the
            # once-a-second interval is running. It alone must notice and
            # clear the toast, well within a couple of ticks.
            page.wait_for_function(
                "() => !document.getElementById('runToast')"
                ".classList.contains('open')", timeout=2000)

            # Re-arm: dismiss again while running.
            page.evaluate(
                "document.getElementById('monitor-body').dataset.status = 'running'")
            page.evaluate("closeRunMonitor()")
            assert toast_open()

            # Reopening must clear it immediately, with no poll tick needed.
            page.evaluate("openRunMonitor()")
            assert not toast_open()
        finally:
            browser.close()


def test_run_now_repolls_immediately_instead_of_waiting_three_seconds(client):
    """Review fix: hx-post="/pipeline/run-now" keeps hx-swap="none" (kept
    from the previous fix round -- see
    test_run_now_discards_the_response_instead_of_blanking_the_monitor above
    -- so the raw "ok" body never blanks the monitor), which meant nothing
    refreshed #monitor-body until the poller's own next 3s tick. Live-
    reproduced: clicking Run Now from an *error* state opened the modal
    showing the *previous* run's stale error, stale stage, and stale
    percentage, under a "Career Agent is running" title, for that whole
    window.

    The reviewer's first-suggested mechanism --
    htmx.trigger('#pipeline-status-poller','load') -- turns out to be a
    no-op here: in the actual shipped htmx 2.0.4 build (unpkg.com/htmx.org@
    2.0.4, vendored below), hx-trigger's "load" keyword is handled by
    addTriggerHandler() as a one-shot initializer gated on
    `!nodeData.firstInitCompleted` that never calls addEventListener --
    unlike e.g. "revealed" or "intersect", which do. So it fires once at
    page load and, critically, registers no listener a later
    htmx.trigger(el, 'load') could ever hit. The fix instead adds a plain
    custom trigger name ("run-started") to the poller's hx-trigger list --
    which *does* go through the normal addEventListener path -- and fires
    that from the button's hx-on::after-request.

    This runs the real, unmodified htmx build against a real DOM in headless
    Chromium (mocking only the two HTTP endpoints, not htmx itself), so a
    wrong event name -- like the original "load" suggestion -- actually
    fails this test, the way a markup-only substring assertion could not."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        pytest.skip(f"playwright not importable: {exc}")

    htmx_js = Path(__file__).parent / "vendor" / "htmx-2.0.4.min.js"
    if not htmx_js.exists():
        pytest.skip("vendored htmx build not available")

    conn = db.connect(web.DB_PATH)
    worker.set_run_state(conn, "pipeline", status="error",
                         last_error="browser crashed", stage="filter")
    conn.commit()
    html = client.get("/").text
    error_fragment = client.get("/pipeline/status").text
    assert "browser crashed" in html  # sanity: the stale error really is there
    assert "browser crashed" in error_fragment

    worker.set_run_state(conn, "pipeline", status="running", last_error=None,
                         stage=None, found=0, duplicates=0, passed=0,
                         scored=0, shortlisted=0)
    conn.execute("UPDATE run_state SET started_at = datetime('now')"
                 " WHERE kind = 'pipeline'")
    conn.commit()
    running_fragment = client.get("/pipeline/status").text
    assert "browser crashed" not in running_fragment
    assert 'data-status="running"' in running_fragment

    # #pipeline-status-poller's own hx-trigger="load, ..." fires a GET the
    # instant the page loads -- before any click -- so a single canned
    # /pipeline/status response can't tell the two apart. Answer that first,
    # automatic poll with the stale error fragment (matching what the real
    # page shows before anyone clicks anything) and only switch to the
    # "running" fragment from the second request on, i.e. the one the click
    # itself must cause.
    status_requests = {"n": 0}

    def handle_status(route):
        status_requests["n"] += 1
        body = error_fragment if status_requests["n"] == 1 else running_fragment
        route.fulfill(status=200, content_type="text/html", body=body)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"no browser available for playwright: {exc}")

        try:
            page = browser.new_page()
            # Fully offline by default; specific mocks below take priority
            # over this since routes run in reverse registration order.
            page.route("**/*", lambda route: route.abort())
            page.route(
                "https://unpkg.com/htmx.org@2.0.4",
                lambda route: route.fulfill(
                    path=str(htmx_js), content_type="application/javascript"))
            page.route(
                "**/pipeline/run-now",
                lambda route: route.fulfill(
                    status=200, content_type="text/html", body="ok"))
            page.route("**/pipeline/status", handle_status)
            # A real (mocked) origin, not set_content()'s about:blank, so the
            # button's relative hx-post/hx-get URLs resolve predictably to
            # something the routes above actually match.
            page.route(
                "https://career-agent.test/",
                lambda route: route.fulfill(
                    status=200, content_type="text/html", body=html))
            t_nav = time.monotonic()  # htmx's "every 3s" interval starts
            # counting from roughly here (page/htmx init), not from the click
            # below -- so the remaining-budget math has to anchor here too.
            page.goto("https://career-agent.test/")
            page.wait_for_function("() => window.htmx !== undefined")
            # Sanity: the page's first paint (inline fragment) plus the
            # automatic first poll (mocked above) both show the stale error
            # -- so the button below really is starting from an error state,
            # not already "running" before the click ever happens.
            page.wait_for_function(
                "() => { const b = document.getElementById('monitor-body');"
                " return !!b && b.dataset.status === 'error'; }")

            # Budget the post-click wait against the real 3s poll interval,
            # not a guessed constant: click() itself carries a real (and, in
            # this sandboxed environment, fairly large -- ~1.5s, measured
            # directly, reproduces even on a bare unrelated button) input-
            # simulation overhead of its own before it even dispatches, and a
            # fixed timeout picked without accounting for that would either
            # be too tight (flaking on a correct fix) or -- worse -- so loose
            # it silently reaches the real 3s mark and starts passing for a
            # BROKEN fix too, once the ambient interval bails it out. Staying
            # a fixed margin under 3000ms regardless of how much click()
            # itself ate keeps the assertion below meaningful either way.
            # (dispatch_event() avoids that overhead but turns out not to
            # reliably reach htmx's click listener the way a real click
            # does -- confirmed empirically: it fell back to the ambient
            # interval instead, ~3s later.)
            page.click("#pipeline-status .btn.primary")
            remaining_ms = max(300, 2700 - (time.monotonic() - t_nav) * 1000)

            page.wait_for_function(
                "() => { const b = document.getElementById('monitor-body');"
                " return !!b && b.dataset.status === 'running'; }",
                timeout=remaining_ms)
            assert "browser crashed" not in page.content()
        finally:
            browser.close()


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


def test_the_apply_activity_log_excludes_pipeline_events(client):
    conn = db.connect(web.DB_PATH)
    for i in range(12):
        conn.execute("INSERT INTO event (job_id, type, payload)"
                     " VALUES (NULL, 'pipeline_progress', ?)", (f"step {i}",))
    conn.execute("INSERT INTO event (job_id, type) VALUES (1, 'human_applied')")
    conn.commit()

    r = client.get("/run/status")

    assert "human_applied" in r.text, (
        "a dozen pipeline rows must not push the apply events out of a "
        "LIMIT 10 log")
    assert "pipeline_progress" not in r.text


def test_marking_applied_creates_the_denominator(client):
    r = client.post("/applied/1", data={"when": "2026-08-20"})
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    row = conn.execute(
        "SELECT status, submitted_at FROM application WHERE job_id = 1"
    ).fetchone()
    assert row["status"] == "submitted"
    assert row["submitted_at"] == "2026-08-20"


def test_marking_applied_defaults_to_today(client):
    client.post("/applied/1", data={"when": ""})
    conn = db.connect(web.DB_PATH)
    today = conn.execute("SELECT date('now') d").fetchone()["d"]
    row = conn.execute(
        "SELECT submitted_at FROM application WHERE job_id = 1").fetchone()
    assert row["submitted_at"] == today


def test_marking_applied_blank_date_anchors_to_utc_not_host_local_clock(
        client, monkeypatch):
    """_parse_date's blank-'when' default must agree with the SQL it feeds --
    worker.guard's and this app's own date(submitted_at) = date('now'),
    which is UTC -- not the host's local calendar day. Simulates the window
    (e.g. ~00:00-05:30 IST in Chennai, UTC+5:30) where local has already
    rolled to the next day but UTC has not, by making the two clocks
    disagree on purpose -- deterministic regardless of what day it actually
    is when the suite runs. If _parse_date read the local clock (the old
    bug), submitted_at would land on 2026-01-02 and the assertion below
    would fail even though this test never touches the real system clock."""
    class _FixedLocalDate(dt.date):
        @classmethod
        def today(cls):
            return dt.date(2026, 1, 2)  # local: already the next day

    class _FixedUTCDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 1, 1, 23, 0, tzinfo=tz)  # UTC: still the 1st

    monkeypatch.setattr(web.dt, "date", _FixedLocalDate)
    monkeypatch.setattr(web.dt, "datetime", _FixedUTCDatetime)

    client.post("/applied/1", data={"when": ""})

    conn = db.connect(web.DB_PATH)
    row = conn.execute(
        "SELECT submitted_at FROM application WHERE job_id = 1").fetchone()
    assert row["submitted_at"] == "2026-01-01"  # UTC's day, not local's


def test_marking_applied_twice_is_refused_readably(client):
    client.post("/applied/1", data={"when": "2026-08-20"})
    r = client.post("/applied/1", data={"when": "2026-08-21"})
    assert r.status_code == 200, "a readable message, not a 500"
    assert "already" in r.text.lower()


def test_a_malformed_date_is_an_error_not_a_422(client):
    r = client.post("/applied/1", data={"when": "last tuesday"})
    assert r.status_code == 200, "friendly error, not FastAPI's raw 422"
    assert "date" in r.text.lower()
    conn = db.connect(web.DB_PATH)
    assert conn.execute(
        "SELECT COUNT(*) n FROM application").fetchone()["n"] == 0


def test_recording_an_outcome_persists_it(client):
    client.post("/applied/1", data={"when": "2026-08-20"})
    conn = db.connect(web.DB_PATH)
    app_id = conn.execute("SELECT id FROM application").fetchone()["id"]

    r = client.post(f"/outcome/{app_id}",
                    data={"type": "screen", "occurred_at": "2026-08-21",
                          "notes": "recruiter call"})
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    row = conn.execute("SELECT type, derived, notes FROM outcome").fetchone()
    assert row["type"] == "screen"
    assert row["derived"] == 0
    assert row["notes"] == "recruiter call"


def test_recording_no_response_by_hand_is_refused(client):
    client.post("/applied/1", data={"when": "2026-08-20"})
    conn = db.connect(web.DB_PATH)
    app_id = conn.execute("SELECT id FROM application").fetchone()["id"]

    r = client.post(f"/outcome/{app_id}",
                    data={"type": "no_response", "occurred_at": "2026-08-21"})
    assert r.status_code == 200
    assert "derived" in r.text.lower()
    conn = db.connect(web.DB_PATH)
    assert conn.execute("SELECT COUNT(*) n FROM outcome").fetchone()["n"] == 0


def test_an_outcome_on_an_unsubmitted_application_is_refused(client):
    """The UI does not offer this, so it guards a forged request."""
    conn = db.connect(web.DB_PATH)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'base-v1', 'draft')")
    conn.commit()
    app_id = conn.execute("SELECT id FROM application").fetchone()["id"]

    r = client.post(f"/outcome/{app_id}",
                    data={"type": "screen", "occurred_at": "2026-08-21"})
    assert r.status_code == 200
    assert "submitted" in r.text.lower()
    conn = db.connect(web.DB_PATH)
    assert conn.execute("SELECT COUNT(*) n FROM outcome").fetchone()["n"] == 0


def test_all_applications_offers_mark_applied_for_an_untracked_job(client):
    r = client.get("/applications")
    assert 'hx-post="/applied/1"' in r.text
    assert "Mark applied" in r.text


def test_all_applications_offers_outcome_controls_once_submitted(client):
    client.post("/applied/1", data={"when": "2026-08-20"})
    conn = db.connect(web.DB_PATH)
    app_id = conn.execute("SELECT id FROM application").fetchone()["id"]

    r = client.get("/applications")
    assert f'hx-post="/outcome/{app_id}"' in r.text
    assert "Awaiting response" in r.text, "no outcome recorded yet"


def test_the_outcome_form_does_not_offer_no_response(client):
    client.post("/applied/1", data={"when": "2026-08-20"})
    r = client.get("/applications")
    outcome_form = r.text.split('hx-post="/outcome/')[1]
    assert 'value="screen"' in outcome_form
    assert 'value="no_response"' not in outcome_form, (
        "derived, never entered")


def test_a_recorded_outcome_is_shown(client):
    """"Interview" is on the page either way -- every MANUAL_TYPES label is an
    <option> in the outcome form -- so this has to assert on the effective
    outcome label the cell leads with, not on the page."""
    client.post("/applied/1", data={"when": "2026-08-20"})
    conn = db.connect(web.DB_PATH)
    app_id = conn.execute("SELECT id FROM application").fetchone()["id"]
    client.post(f"/outcome/{app_id}",
                data={"type": "interview", "occurred_at": "2026-08-21"})

    r = client.get("/applications")
    label = r.text.split('class="outcome-cell"')[1].split("</span>")[0]
    assert "Interview" in label
    assert "Awaiting response" not in r.text, "an outcome exists now"



def test_marking_applied_clears_the_run_state_so_the_worker_can_advance(
        client, monkeypatch):
    """Manual mode parks the run on a draft, and marking applied is now the
    sanctioned way to resolve one. If it doesn't release current_job_id,
    apply_tick returns early forever and the run is dead."""
    async def fake_submit(conn, job_id, dry_run, filler=None):
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'draft')", (job_id,))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)
    client.post("/run/start", data={"mode": "manual"})
    conn = db.connect(web.DB_PATH)
    assert worker.get_run_state(conn, "apply")["current_job_id"] == 1

    r = client.post("/applied/1", data={"when": "2026-08-20"})
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    assert worker.get_run_state(conn, "apply")["current_job_id"] is None


def test_send_unparks_the_run_when_submission_is_not_implemented(client):
    """The real refusal path, with no monkeypatched submit: production always
    takes it (SUBMISSION_IMPLEMENTED is False), so unparking only on ok would
    leave the run parked forever on a job Send can never resolve."""
    conn = db.connect(web.DB_PATH)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'base-v1', 'draft')")
    conn.commit()
    worker.set_run_state(conn, "apply", status="running", mode="manual",
                         current_job_id=1)

    r = client.post("/send/1")
    assert r.status_code == 200
    assert "not implemented" in r.text.lower()
    conn = db.connect(web.DB_PATH)
    assert worker.get_run_state(conn, "apply")["current_job_id"] is None


def test_send_stays_parked_on_a_transient_failure(client, monkeypatch):
    """A captcha hold or an errored filler can succeed on a retry, so the run
    keeps pointing at that draft -- only a categorical refusal unparks."""
    async def refuses(conn, job_id, dry_run, filler=None):
        return {"ok": False, "held": True,
                "reason": "captcha encountered; held for review"}

    monkeypatch.setattr(web.ats_apply, "submit", refuses)
    conn = db.connect(web.DB_PATH)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'base-v1', 'draft')")
    conn.commit()
    worker.set_run_state(conn, "apply", status="running", mode="manual",
                         current_job_id=1)

    r = client.post("/send/1")
    assert "captcha" in r.text.lower()
    conn = db.connect(web.DB_PATH)
    assert worker.get_run_state(conn, "apply")["current_job_id"] == 1


def test_the_skipped_tab_offers_outcome_controls_for_an_applied_job(client):
    """Job 2 is the skip-verdict fixture. An override is the most valuable
    label the system produces, so it has to be able to reach the callback
    denominator from the tab it lives in."""
    client.post("/applied/2", data={"when": "2026-08-20"})
    conn = db.connect(web.DB_PATH)
    app_id = conn.execute(
        "SELECT id FROM application WHERE job_id = 2").fetchone()["id"]

    skipped = client.get("/applications").text.split('id="tab-skipped"')[1]
    assert f'hx-post="/outcome/{app_id}"' in skipped


def test_the_skipped_tab_offers_mark_applied_and_keeps_track_anyway(client):
    skipped = client.get("/applications").text.split('id="tab-skipped"')[1]
    assert 'hx-post="/applied/2"' in skipped
    assert "Track anyway" in skipped


def test_marking_applied_says_when_it_hits_the_daily_cap(client, brief_path):
    """A hand-marked row counts against daily_cap like any other and the run
    auto-pauses at it, so the click that caused the pause has to say so."""
    brief_path.write_text(BRIEF_TOML.replace("daily_cap = 5", "daily_cap = 1"),
                          encoding="utf-8")
    r = client.post("/applied/1", data={"when": ""})
    assert "Marked applied" in r.text
    assert "daily cap of 1 reached" in r.text


def test_marking_applied_stays_quiet_below_the_daily_cap(client, brief_path):
    r = client.post("/applied/1", data={"when": ""})
    assert "Marked applied" in r.text
    assert "daily cap" not in r.text
