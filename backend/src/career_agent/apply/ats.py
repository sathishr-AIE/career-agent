"""The apply engine's DB half: `submit()` owns the application state
machine and every write; `apply/agent.py` owns the browser session that
decides an outcome. See docs/lld-apply-button-v2.md section 5."""
import datetime as dt
import json
import logging
import sqlite3
from pathlib import Path

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
# (stuck, page_error, login_issue, free text) is retryable until
# MAX_ATTEMPTS. See docs/lld-apply-button-v2.md section 5.2.
PERMANENT_REASONS = {
    "expired", "sso_required", "easy_apply", "naukri_platform",
    "not_eligible_location", "already_applied", "not_a_job_application",
    "unsafe_permissions", "unsafe_verification", "site_blocked",
}

# Reasons that mean the agent drove a real browser and then stopped
# reporting: it may have clicked Submit before it died. ON THE SEND PATH
# these are neither "failed" nor "permanent" -- the application's true
# state is UNKNOWN, so they become 'held_unknown' (a BLOCKING status) and
# a human adjudicates, exactly as sweep_stale_in_flight does for a crashed
# in_flight row (see docs/lld-apply-button-v2.md section 6, layer 2).
# Retrying instead would be a double-submit vector the moment
# SUBMISSION_IMPLEMENTED flips. The DRAFT path keeps them retryable:
# nothing is submitted at draft time, so re-drafting is free and correct.
# Matched on the part before the first ':' -- "unrecognized_result:<body>"
# carries a payload.
UNKNOWN_STATE_REASONS = {"agent_error", "timeout", "no_result_line",
                         "unrecognized_result"}

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


def is_unknown_state(reason: str) -> bool:
    return reason.split(":", 1)[0].strip() in UNKNOWN_STATE_REASONS


def sweep_stale_in_flight(conn: sqlite3.Connection, minutes: int = 15) -> int:
    """A crash during submission leaves in_flight behind. Its true state is
    unknown, so it blocks rather than allowing a possible double send."""
    cur = conn.execute(
        "UPDATE application SET status = 'held_unknown'"
        " WHERE status = 'in_flight'"
        f"  AND started_at < datetime('now', '-{int(minutes)} minutes')")
    conn.commit()
    return cur.rowcount


def preflight() -> None:
    """Everything _live_run_agent needs before it can do anything at all
    (spec 5.1 guards). Raises agent.PreconditionError -- submit() calls
    this BEFORE the in_flight INSERT, because a missing binary means no
    browser launched and nothing submitted: recording it as a failure at
    all (and on the send path as held_unknown, which BLOCKS) would convert
    the whole queue to a stuck state over a PATH problem."""
    from career_agent.apply import chrome as chrome_mod

    agent_mod.require_binaries()
    try:
        chrome_mod.get_chrome_path()
    except RuntimeError as exc:   # "Chrome not found -- set CHROME_PATH"
        raise agent_mod.PreconditionError(str(exc)) from exc


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
                          result, detail: str = "") -> dict:
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
        # Never falsy: worker.apply_tick parks on `if result.get(
        # "needs_answer")`, so a bare RESULT:NEEDS_ANSWER: with no question
        # text would fall through to job_skipped and clear the very park
        # this mechanism exists to set.
        question = result.reason or "(question not reported)"
        return {"ok": False, "needs_answer": question,
                "reason": f"needs an answer: {question}"}

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
        # No UNKNOWN_STATE_REASONS special case here on purpose: a draft
        # submits nothing, so a crashed/timed-out/unparseable draft run is
        # simply retryable.
        reason = _reason_of(result)
        status = classify_failure(reason, _prior_failures(conn, job_id))

    conn.execute(
        "INSERT INTO application (job_id, resume_version, answers, status,"
        " failure_reason, transcript_path) VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, resume_version, json.dumps(result.answers or {}), status,
         reason, result.transcript_path or None))
    conn.execute("INSERT INTO event (job_id, type, payload) VALUES (?, ?, ?)",
                 (job_id, status, detail or reason))
    conn.commit()
    return {"ok": False, "reason": f"draft {status}: {detail or reason}"}


def _record_send_outcome(conn, job_id: int, app_id: int, url: str,
                         pinned: dict | None, result, detail: str = "") -> dict:
    code = result.code

    if code == "applied":
        # pinned wins when the agent reports nothing back -- an empty
        # ANSWERS_JSON included. The review invariant says the recorded
        # answers are what was reviewed, so `{}` must not overwrite them.
        answers = result.answers or pinned or {}
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
        question = result.reason or "(question not reported)"
        return {"ok": False, "needs_answer": question,
                "reason": f"needs an answer: {question}"}

    # expired / login_issue / failed / draft_ready-in-send-mode / anything
    # unrecognized: the in_flight row becomes the failure record. A reason
    # that means "the agent stopped reporting mid-run" holds instead of
    # failing -- it may already have submitted (see UNKNOWN_STATE_REASONS).
    reason = _reason_of(result)
    status = ("held_unknown" if is_unknown_state(reason)
              else classify_failure(reason, _prior_failures(conn, job_id)))
    # `AND status = 'in_flight'` closes a double-submit race: timeout_s is
    # not a wall-clock bound, so a run can outlive sweep_stale_in_flight's
    # 15 minutes and come back to find its own row already held_unknown.
    # Overwriting that with 'failed' (not BLOCKING) would re-admit the job
    # to QUEUE_WHERE and apply a second time to a form this run may
    # already have submitted. The 'applied' branch above stays
    # unconditional on purpose -- held_unknown -> submitted is the
    # truthful upgrade and must never be suppressed.
    conn.execute(
        "UPDATE application SET status = ?, failure_reason = ?,"
        " transcript_path = ? WHERE id = ? AND status = 'in_flight'",
        (status, reason, result.transcript_path or None, app_id))
    conn.execute("INSERT INTO event (job_id, type, payload) VALUES (?, ?, ?)",
                 (job_id, status, detail or reason))
    conn.commit()
    return {"ok": False, "reason": f"submission {status}: {detail or reason}"}


async def _run(runner, prompt: str, job_id: int) -> tuple:
    """run_agent does not return an AgentResult on every path -- a broken
    stdin pipe or a missing `claude`/`npx` binary raises out of it. Turn
    that into a recordable result so the caller always has one, and never
    leaves an in_flight row stranded.

    Returns (result, detail). The exception text goes in `detail`, for the
    event payload, so `failure_reason` stays the queryable taxonomy slug
    the schema promises (spec section 5.3)."""
    try:
        return await runner(prompt, job_id), ""
    except agent_mod.PreconditionError as exc:
        # submit()'s preflight normally catches this first; reaching here
        # means the backstop inside _run_agent_blocking fired. Nothing
        # launched, so it is a plain retryable failure -- never
        # 'agent_error', which would hold the row as possibly-submitted.
        log.warning("apply preconditions failed for job %s: %s", job_id, exc)
        return agent_mod.AgentResult("failed", "precondition"), str(exc)
    except Exception as exc:
        log.exception("apply agent crashed for job %s", job_id)
        return agent_mod.AgentResult("failed", "agent_error"), str(exc)


async def submit(conn: sqlite3.Connection, job_id: int, dry_run: bool,
                 brief: CareerBrief,
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

    if run_agent is None:
        # Preconditions describe what _live_run_agent needs, so they are
        # checked only when it is the runner (an injected one is the test
        # seam, same rule as the kill switch above). Before any write: a
        # missing binary records nothing at all.
        try:
            preflight()
        except agent_mod.PreconditionError as exc:
            # `unsupported` is reused from the kill-switch refusal (the
            # other "ok: False and no application row" case) precisely
            # because it is what makes worker.apply_tick pause the run
            # instead of clearing current_job_id: with no row written,
            # QUEUE_WHERE re-admits this job and the next tick picks it
            # again. A missing binary cannot be retried away either, so
            # pausing once with the reason is the right stop.
            return {"ok": False, "unsupported": True, "reason": str(exc)}

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

    # tailor.py stores resume.path relative to backend/, but the agent runs
    # with a cwd outside the repo, so the path it is told to upload -- and
    # that the Playwright server opens -- must be absolute or it resolves
    # to nothing at the one step this whole feature exists for. Same bug
    # class as the relative --mcp-config path. Only the prompt's copy is
    # absolutised: _resume_text below reads the stored path from THIS
    # process, whose cwd is backend/, where it already resolves.
    resume_path = Path(resume_row["path"]).resolve()
    if not resume_path.exists():
        # Refuse rather than start a session that can only fail at the
        # upload: same spirit as preflight(), same no-row rule, and
        # `unsupported` for the same worker reason (a refusal that writes
        # no row would otherwise be re-picked on the next tick, forever).
        return {"ok": False, "unsupported": True, "reason":
                f"résumé file for version {resume_version!r} is missing at"
                f" {resume_path}"}

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
                   _resume_text(resume_row), str(resume_path))
    score = score_row["weighted_score"] if score_row else None
    runner = run_agent or _live_run_agent

    if dry_run:
        prompt = agent_mod.build_prompt(*prompt_args, mode="draft", score=score)
        result, detail = await _run(runner, prompt, job_id)
        return _record_draft_outcome(conn, job_id, resume_version,
                                     row["url"], result, detail)

    # Real send. A draft on record means a human (or auto mode's own draft
    # pass) already reviewed those answers, and they are what must be sent
    # -- never recomputed. No draft means auto mode's single pass, where
    # the agent decides and submits in one session.
    draft = conn.execute(
        "SELECT answers FROM application WHERE job_id = ? AND status = 'draft'"
        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    pinned = None
    if draft is not None:
        # A draft exists => a human review is expected, so send mode is
        # non-negotiable even if that draft recorded no answers. Falling
        # through to auto mode there would let the agent improvise and
        # submit answers nobody saw.
        #
        # Reused verbatim, with no re-check of qa_bank's 30-day volatility
        # window even if the draft is older than that window. Revalidating
        # here would contradict the whole point of reuse -- "what was shown
        # for review is exactly what gets sent" -- by silently sending
        # different answers than what was reviewed. Reuse wins.
        pinned = json.loads(draft["answers"] or "{}")
        prompt = agent_mod.build_prompt(*prompt_args, mode="send",
                                        pinned_answers=pinned, score=score)
    else:
        prompt = agent_mod.build_prompt(*prompt_args, mode="auto", score=score)

    cur = conn.execute(
        "INSERT INTO application (job_id, resume_version, status, started_at)"
        " VALUES (?, ?, 'in_flight', datetime('now'))", (job_id, resume_version))
    app_id = cur.lastrowid
    conn.commit()

    result, detail = await _run(runner, prompt, job_id)
    return _record_send_outcome(conn, job_id, app_id, row["url"], pinned,
                                result, detail)
