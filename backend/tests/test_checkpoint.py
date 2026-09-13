import json

import pytest

from career_agent import db
from career_agent.apply import checkpoint


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    c.execute("INSERT INTO job (fingerprint, source, external_id, company, company_normalized,"
              " title, title_normalized, url) VALUES ('fp','ats','1','Acme','acme','AI',"
              " 'ai','https://x')")
    c.commit()
    return c


def test_transitions_start_waiting_running_done(conn):
    assert checkpoint.get(conn, 1) is None
    checkpoint.start(conn, 1, "sess-1", "n0nce")
    cp = checkpoint.get(conn, 1)
    assert (cp["status"], cp["step"], cp["answers"], cp["session_id"], cp["nonce"]) == (
        "running", "start", {}, "sess-1", "n0nce")

    checkpoint.mark_waiting(conn, 1, 7)
    cp = checkpoint.get(conn, 1)
    assert (cp["status"], cp["open_prompt_id"]) == ("waiting", 7)

    checkpoint.mark_running(conn, 1, "answered q1", {"Notice?": "30"})
    checkpoint.mark_running(conn, 1, "answered q2", {"Visa?": "Citizen", "Notice?": "60"})
    cp = checkpoint.get(conn, 1)
    assert cp["status"] == "running" and cp["step"] == "answered q2"
    assert cp["answers"] == {"Notice?": "60", "Visa?": "Citizen"}    # merged
    assert cp["open_prompt_id"] is None

    checkpoint.finish(conn, 1)
    assert checkpoint.get(conn, 1)["status"] == "done"


def test_a_fresh_start_resets_the_previous_run(conn):
    checkpoint.start(conn, 1, "old", "n1")
    checkpoint.mark_running(conn, 1, "answered q1", {"a": "b"})
    checkpoint.mark_resumable(conn, 1)
    checkpoint.start(conn, 1, "new", "n2")
    cp = checkpoint.get(conn, 1)
    assert (cp["session_id"], cp["nonce"], cp["answers"], cp["status"]) == ("new", "n2", {}, "running")


def test_resumable_and_sweep_orphans(conn):
    conn.execute("INSERT INTO job (fingerprint, source, external_id, company, company_normalized,"
                 " title, title_normalized, url) VALUES ('fp2','ats','2','B','b','AI','ai','https://y')")
    checkpoint.start(conn, 1, "s1", "n", mode="manual", can_submit=True)
    checkpoint.start(conn, 2, "s2", "n", mode="manual", can_submit=True)
    checkpoint.mark_waiting(conn, 2, 3)
    assert checkpoint.sweep_orphans(conn, live_job_ids={2}) == 1     # 2 is still driven
    assert checkpoint.get(conn, 1)["status"] == "resumable"
    assert checkpoint.get(conn, 2)["status"] == "waiting"
    checkpoint.finish(conn, 2)
    assert checkpoint.sweep_orphans(conn, set()) == 0                # done is never swept


def test_continue_message(conn):
    checkpoint.start(conn, 1, "s1", "abc")
    checkpoint.mark_running(conn, 1, "answered q1", {"Notice?": "30"})
    msg = checkpoint.continue_message(checkpoint.get(conn, 1), "abc")
    first, rest = msg.split("\n", 1)
    assert first.startswith("CONTINUE:abc:")
    assert json.loads(first.split(":", 2)[2]) == {"step": "answered q1", "answers": {"Notice?": "30"}}
    assert "You were interrupted" in rest and "emit it again now" in rest
    assert "PREVIOUSLY ANSWERED" in rest


def test_a_late_answer_never_reopens_a_finished_checkpoint(conn):
    """M2: answer_prompt checkpoints after run.send; the run may finish first."""
    checkpoint.start(conn, 1, "s", "n")
    checkpoint.mark_waiting(conn, 1, 3)
    checkpoint.finish(conn, 1)                          # the outcome lands first
    checkpoint.mark_running(conn, 1, "answered 3", {"q": "a"})
    cp = checkpoint.get(conn, 1)
    assert (cp["status"], cp["answers"]) == ("done", {})
    checkpoint.mark_resumable(conn, 1)
    checkpoint.mark_running(conn, 1, "answered 3", {"q": "a"})
    assert checkpoint.get(conn, 1)["status"] == "resumable"
    checkpoint.resume(conn, 1)
    assert (checkpoint.get(conn, 1)["status"], checkpoint.get(conn, 1)["step"]) == ("running", "resumed")


def test_sweep_orphans_finishes_a_job_whose_latest_attempt_is_terminal(conn):
    """M6: a lost finish() write must not make a finished job resumable on restart."""
    checkpoint.start(conn, 1, "s", "n")
    conn.execute("INSERT INTO application (job_id, resume_version, status) VALUES (1, 'v', 'draft')")
    conn.commit()
    assert checkpoint.sweep_orphans(conn, set()) == 0
    assert checkpoint.get(conn, 1)["status"] == "done"
    checkpoint.start(conn, 1, "s", "n", mode="manual")  # a live attempt's row: in_flight
    conn.execute("INSERT INTO application (job_id, resume_version, status) VALUES (1, 'v', 'in_flight')")
    conn.commit()
    assert checkpoint.sweep_orphans(conn, set()) == 1
    assert checkpoint.get(conn, 1)["status"] == "resumable"


# -- Task 11 ------------------------------------------------------------------

def test_start_records_mode_and_can_submit_and_resets_the_counters(conn):
    checkpoint.start(conn, 1, "s", "n", mode="auto", can_submit=True)
    checkpoint.mark_approve_sent(conn, 1)
    checkpoint.mark_resumable(conn, 1)
    checkpoint.resume(conn, 1)
    cp = checkpoint.get(conn, 1)
    assert (cp["mode"], cp["can_submit"], cp["approve_sent"], cp["resume_count"]) == ("auto", 1, 1, 1)
    checkpoint.start(conn, 1, "s2", "n2", mode="manual", can_submit=False)
    cp = checkpoint.get(conn, 1)
    assert (cp["mode"], cp["can_submit"], cp["approve_sent"], cp["resume_count"], cp["auto_resumed"]) == (
        "manual", 0, 0, 0, 0)


def test_restart_keeps_the_resume_count_and_answers(conn):
    checkpoint.start(conn, 1, "s", "n", mode="manual", can_submit=True)
    checkpoint.mark_running(conn, 1, "answered 1", {"q": "a"})
    checkpoint.mark_resumable(conn, 1)
    checkpoint.resume(conn, 1)
    checkpoint.restart(conn, 1, "s2", "n2")
    cp = checkpoint.get(conn, 1)
    assert (cp["session_id"], cp["nonce"], cp["resume_count"], cp["answers"], cp["status"]) == (
        "s2", "n2", 1, {"q": "a"}, "running")


def test_sweep_never_makes_a_session_that_sent_an_approve_resumable(conn):
    """A crashed session after DECISION approve may have clicked Submit."""
    conn.execute("INSERT INTO job (fingerprint, source, external_id, company, company_normalized,"
                 " title, title_normalized, url) VALUES ('fp2','ats','2','B','b','AI','ai','https://y')")
    checkpoint.start(conn, 1, "s", "n", mode="manual", can_submit=True)
    checkpoint.mark_approve_sent(conn, 1)
    checkpoint.start(conn, 2, "s", "n", mode="manual", can_submit=False)   # a draft: nothing sendable
    checkpoint.mark_approve_sent(conn, 2)
    assert checkpoint.sweep_orphans(conn, set()) == 1
    assert checkpoint.get(conn, 1)["status"] == "done"
    assert checkpoint.get(conn, 2)["status"] == "resumable"


def test_next_auto_resume_picks_each_checkpoint_once(conn):
    checkpoint.start(conn, 1, "s", "n", mode="auto", can_submit=True)
    assert checkpoint.next_auto_resume(conn) is None           # running, not resumable
    checkpoint.mark_resumable(conn, 1)
    assert checkpoint.next_auto_resume(conn) == 1
    checkpoint.set_auto_resumed(conn, 1, True)
    assert checkpoint.next_auto_resume(conn) is None
    checkpoint.set_auto_resumed(conn, 1, False)                # a human touch re-arms it
    assert checkpoint.next_auto_resume(conn) == 1
    checkpoint.start(conn, 1, "s", "n", mode="manual", can_submit=True)   # I1: never a manual one
    checkpoint.mark_resumable(conn, 1)
    assert checkpoint.next_auto_resume(conn) is None


def test_sweep_finishes_a_crashed_auto_session_that_could_submit(conn):
    """I3: an auto session is pre-approved; it can click Submit before approve_sent."""
    conn.execute("INSERT INTO job (fingerprint, source, external_id, company, company_normalized,"
                 " title, title_normalized, url) VALUES ('fp2','ats','2','B','b','AI','ai','https://y')")
    checkpoint.start(conn, 1, "s", "n", mode="auto", can_submit=True)
    checkpoint.start(conn, 2, "s", "n", mode="auto", can_submit=False)    # kill switch off
    assert checkpoint.sweep_orphans(conn, set()) == 1
    assert checkpoint.get(conn, 1)["status"] == "done"
    assert checkpoint.get(conn, 2)["status"] == "resumable"
