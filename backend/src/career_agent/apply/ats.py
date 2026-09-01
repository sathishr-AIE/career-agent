"""The apply engine's DB half: `submit()` owns the application state
machine and every write; `apply/agent.py` owns the browser session that
decides an outcome. See docs/lld-apply-button-v2.md section 5."""
import datetime as dt
import json
import logging
import sqlite3

from career_agent.apply import agent as agent_mod
from career_agent.config import CandidateProfile, CareerBrief
from career_agent.models import Job

log = logging.getLogger(__name__)

RESUME_VERSION = "base-v1"
MAX_ATTEMPTS = 3
BLOCKING = ("in_flight", "submitted", "held_unknown", "failed_permanent")

# The outermost kill switch: a real send is refused unless this is flipped
# by hand, for EVERY source (the agent engine is source-agnostic, so the
# old per-source refusal is gone and this flag is the only gate left). See
# the "Rollout" section of
# docs/superpowers/specs/2026-08-23-auto-submission-design.md. Drafting is
# not gated -- it fills a form without submitting it.
SUBMISSION_IMPLEMENTED = False

# Reasons a retry can never fix: the posting is gone, the platform is one
# we refuse on principle, or the candidate is not eligible. Anything else
# (stuck, page_error, timeout, login_issue, free text) is retryable until
# MAX_ATTEMPTS. See docs/lld-apply-button-v2.md section 5.2.
PERMANENT_REASONS = {
    "expired", "sso_required", "easy_apply", "naukri_platform",
    "not_eligible_location", "already_applied", "not_a_job_application",
    "unsafe_permissions", "unsafe_verification", "site_blocked",
}

QA_VOLATILE_WINDOW_DAYS = 30


def _confirmed_within_days(last_confirmed_at: str, days: int) -> bool:
    """last_confirmed_at is a SQLite datetime('now') string: naive, UTC.
    Comparing against a naive-UTC "now" keeps both sides in the same clock --
    this project has hit local-vs-UTC datetime mismatches as a recurring bug
    before, so this stays naive-UTC on purpose rather than using a
    timezone-aware "now" (dt.datetime.now(dt.UTC).replace(tzinfo=None) is
    just dt.datetime.utcnow() without the deprecation warning)."""
    confirmed = dt.datetime.fromisoformat(last_confirmed_at)
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    return (now - confirmed) <= dt.timedelta(days=days)


def classify_failure(reason: str, prior_failures: int) -> str:
    if reason in PERMANENT_REASONS:
        return "failed_permanent"
    return "failed_permanent" if prior_failures + 1 >= MAX_ATTEMPTS else "failed"


def sweep_stale_in_flight(conn: sqlite3.Connection, minutes: int = 15) -> int:
    """A crash during submission leaves in_flight behind. Its true state is
    unknown, so it blocks rather than allowing a possible double send."""
    cur = conn.execute(
        "UPDATE application SET status = 'held_unknown'"
        " WHERE status = 'in_flight'"
        f"  AND started_at < datetime('now', '-{int(minutes)} minutes')")
    conn.commit()
    return cur.rowcount


async def _live_run_agent(prompt: str, job_id: int):
    """Default agent runner: a real Chrome around a real `claude` session.
    Tests inject their own run_agent instead -- nothing in the test suite
    ever reaches this, by house convention (no test spawns a browser or a
    subprocess)."""
    from career_agent.apply import chrome as chrome_mod

    proc = chrome_mod.launch_chrome()
    try:
        return await agent_mod.run_agent(prompt, job_id=job_id)
    finally:
        chrome_mod.cleanup(proc)


def _resume_text(row) -> str:
    """Body text for the prompt's RESUME TEXT section.

    resume.content is tailor.py's JSON -- the tailored summary and bullets
    only, not the work history an agent filling a Workday/iCIMS employment
    section needs -- so the body comes off the rendered DOCX and the
    tailored parts are appended. Degrades rather than raising: an
    unreadable file falls back to the JSON, then to ""."""
    parts = []
    try:
        import docx

        doc = docx.Document(row["path"])
        body = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        if body:
            parts.append(body)
    except Exception:
        log.debug("could not read resume docx at %r", row["path"], exc_info=True)

    content = row["content"]
    if content:
        try:
            data = json.loads(content)
            tailored = [data["summary"]] if data.get("summary") else []
            tailored += [b.get("text", "") for b in data.get("bullets", [])]
            tailored = [t for t in tailored if t]
        except (json.JSONDecodeError, AttributeError, TypeError):
            tailored = [str(content)]
        if tailored:
            parts.append("Tailored for this job:\n"
                         + "\n".join(f"- {t}" for t in tailored))
    return "\n\n".join(parts)


def _reason_of(result) -> str:
    """AgentResult("expired") carries no reason; the code itself is the
    taxonomy slug in that case."""
    return result.reason or result.code


def _prior_failures(conn, job_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) n FROM application"
        " WHERE job_id = ? AND status = 'failed'", (job_id,)).fetchone()["n"]


def _record_draft_outcome(conn, job_id: int, resume_version: str, url: str,
                          result) -> dict:
    code = result.code

    if code == "draft_ready":
        conn.execute(
            "INSERT INTO application (job_id, resume_version, answers, status,"
            " transcript_path) VALUES (?, ?, ?, 'draft', ?)",
            (job_id, resume_version, json.dumps(result.answers or {}),
             result.transcript_path or None))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    if code == "needs_answer":
        return {"ok": False, "needs_answer": result.reason,
                "reason": f"needs an answer: {result.reason}"}

    if code == "captcha":
        conn.execute("INSERT INTO event (job_id, type, payload)"
                     " VALUES (?, 'captcha_held', ?)",
                     (job_id, result.transcript_path or url))
        conn.commit()
        return {"ok": False, "held": True,
                "reason": "captcha encountered; held for review"}

    if code == "applied":
        # Draft mode explicitly forbids clicking Submit. If the agent says
        # it applied anyway, the true state is unknown and possibly
        # submitted -- hold it (a BLOCKING status) so no later run can send
        # a second time, rather than filing a retryable failure.
        status, reason = "held_unknown", "applied_during_draft"
    else:
        reason = _reason_of(result)
        status = classify_failure(reason, _prior_failures(conn, job_id))

    conn.execute(
        "INSERT INTO application (job_id, resume_version, answers, status,"
        " failure_reason, transcript_path) VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, resume_version, json.dumps(result.answers or {}), status,
         reason, result.transcript_path or None))
    conn.execute("INSERT INTO event (job_id, type, payload) VALUES (?, ?, ?)",
                 (job_id, status, reason))
    conn.commit()
    return {"ok": False, "reason": f"draft {status}: {reason}"}


def _record_send_outcome(conn, job_id: int, app_id: int, url: str,
                         pinned: dict | None, result) -> dict:
    code = result.code

    if code == "applied":
        # pinned wins when the agent reports nothing back: the review
        # invariant says the recorded answers are what was reviewed.
        answers = result.answers if result.answers is not None else (pinned or {})
        conn.execute(
            "UPDATE application SET status = 'submitted', answers = ?,"
            " submitted_at = datetime('now'), transcript_path = ?"
            " WHERE id = ?",
            (json.dumps(answers), result.transcript_path or None, app_id))
        conn.execute("INSERT INTO event (job_id, type, payload)"
                     " VALUES (?, 'submitted', ?)", (job_id, url))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "submitted"}

    if code == "captcha":
        conn.execute("DELETE FROM application WHERE id = ?", (app_id,))
        conn.execute("INSERT INTO event (job_id, type, payload)"
                     " VALUES (?, 'captcha_held', ?)", (job_id, url))
        conn.commit()
        return {"ok": False, "held": True,
                "reason": "captcha encountered; held for review"}

    if code == "needs_answer":
        # Nothing was sent, so the attempt leaves no trace: the worker
        # parks on the question and the job re-enters the queue once it is
        # answered.
        conn.execute("DELETE FROM application WHERE id = ?", (app_id,))
        conn.commit()
        return {"ok": False, "needs_answer": result.reason,
                "reason": f"needs an answer: {result.reason}"}

    # expired / login_issue / failed / draft_ready-in-send-mode / anything
    # unrecognized: the in_flight row becomes the failure record.
    reason = _reason_of(result)
    status = classify_failure(reason, _prior_failures(conn, job_id))
    conn.execute(
        "UPDATE application SET status = ?, failure_reason = ?,"
        " transcript_path = ? WHERE id = ?",
        (status, reason, result.transcript_path or None, app_id))
    conn.execute("INSERT INTO event (job_id, type, payload) VALUES (?, ?, ?)",
                 (job_id, status, reason))
    conn.commit()
    return {"ok": False, "reason": f"submission {status}: {reason}"}


async def _run(runner, prompt: str, job_id: int):
    """run_agent does not return an AgentResult on every path -- a broken
    stdin pipe or a missing `claude`/`npx` binary raises out of it. Turn
    that into a retryable failure so the caller always has a result to
    record, and never leaves an in_flight row stranded."""
    try:
        return await runner(prompt, job_id)
    except Exception as exc:
        log.exception("apply agent crashed for job %s", job_id)
        return agent_mod.AgentResult("failed", f"agent_error: {exc}")


async def submit(conn: sqlite3.Connection, job_id: int, dry_run: bool,
                 brief: CareerBrief | None = None,
                 profile: CandidateProfile | None = None,
                 resume_version: str | None = None,
                 run_agent=None) -> dict:
    """Draft (dry_run=True) or really send (dry_run=False) one application,
    by running one apply-agent session and translating its AgentResult into
    this module's state machine. `run_agent` is the only test seam:
    `async (prompt: str, job_id: int) -> AgentResult`."""
    row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason": f"job {job_id} not found"}

    # Kill switch first, before anything is written. It now gates a real
    # send for every source, not just Greenhouse -- an injected run_agent
    # (tests only) is the escape hatch, same as the old injected filler.
    if not dry_run and run_agent is None and not SUBMISSION_IMPLEMENTED:
        return {"ok": False, "unsupported": True, "reason":
                "The agentic apply engine is built but real sends are not"
                " enabled yet (SUBMISSION_IMPLEMENTED). Apply on the site"
                " yourself, then record the outcome."}

    live = conn.execute(
        f"SELECT status FROM application WHERE job_id = ? AND status IN "
        f"({','.join('?' * len(BLOCKING))})", (job_id, *BLOCKING)).fetchone()
    if live:
        return {"ok": False,
                "reason": f"job {job_id} already has a {live['status']} attempt"}

    if profile is None:
        raise RuntimeError(
            "candidate_profile.toml not found -- see"
            " candidate_profile.toml.example. The apply agent needs it for"
            " every source.")

    resume_version = resume_version or RESUME_VERSION
    resume_row = conn.execute(
        "SELECT path, content FROM resume WHERE version = ?",
        (resume_version,)).fetchone()
    if resume_row is None:
        # Required for a draft too, not just a send: the agent uploads the
        # file during the draft run, so a missing one has to fail here
        # rather than produce a prompt pointing at nothing.
        return {"ok": False,
                "reason": f"no résumé on record for version {resume_version!r}"}

    from career_agent import store  # local: store.py imports this module
                                    # for RESUME_VERSION, so a top-level
                                    # import back would be circular.
    job = Job(source=row["source"], external_id=row["external_id"],
              company=row["company"], title=row["title"],
              location=row["location"], is_remote=bool(row["is_remote"]),
              comp_min=row["comp_min"], comp_max=row["comp_max"],
              posted_at=row["posted_at"], url=row["url"],
              description=row["description"])
    score_row = conn.execute(
        "SELECT weighted_score FROM assessment WHERE job_id = ?"
        " ORDER BY created_at DESC, id DESC LIMIT 1", (job_id,)).fetchone()
    prompt_args = (job, profile, brief, store.qa_all(conn),
                   _resume_text(resume_row), resume_row["path"])
    score = score_row["weighted_score"] if score_row else None
    runner = run_agent or _live_run_agent

    if dry_run:
        prompt = agent_mod.build_prompt(*prompt_args, mode="draft", score=score)
        result = await _run(runner, prompt, job_id)
        return _record_draft_outcome(conn, job_id, resume_version,
                                     row["url"], result)

    # Real send. A draft on record means a human (or auto mode's own draft
    # pass) already reviewed those answers, and they are what must be sent
    # -- never recomputed. No draft means auto mode's single pass, where
    # the agent decides and submits in one session.
    draft = conn.execute(
        "SELECT answers FROM application WHERE job_id = ? AND status = 'draft'"
        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    pinned = None
    if draft is not None and draft["answers"]:
        # Reused verbatim, with no re-check of qa_bank's 30-day volatility
        # window even if the draft is older than that window. Revalidating
        # here would contradict the whole point of reuse -- "what was shown
        # for review is exactly what gets sent" -- by silently sending
        # different answers than what was reviewed. Reuse wins.
        pinned = json.loads(draft["answers"])
        prompt = agent_mod.build_prompt(*prompt_args, mode="send",
                                        pinned_answers=pinned, score=score)
    else:
        prompt = agent_mod.build_prompt(*prompt_args, mode="auto", score=score)

    cur = conn.execute(
        "INSERT INTO application (job_id, resume_version, status, started_at)"
        " VALUES (?, ?, 'in_flight', datetime('now'))", (job_id, resume_version))
    app_id = cur.lastrowid
    conn.commit()

    result = await _run(runner, prompt, job_id)
    return _record_send_outcome(conn, job_id, app_id, row["url"], pinned, result)
