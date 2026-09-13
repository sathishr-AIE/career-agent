"""The apply engine's DB half: `submit()` owns the application state
machine and every write; `apply/agent.py` owns the browser session that
decides an outcome. See docs/lld-apply-button-v2.md section 5."""
import asyncio
import datetime as dt
import json
import logging
import re
import shutil
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
# MAX_ATTEMPTS -- account_required included on purpose: it needs one human
# action (create the account / give the consent in the agent's Chrome
# profile), after which the same job should be re-attempted. See
# docs/lld-apply-button-v2.md section 5.2.
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


def _blocking_status(conn, job_id: int):
    return conn.execute(
        f"SELECT status FROM application WHERE job_id = ? AND status IN "
        f"({','.join('?' * len(BLOCKING))})", (job_id, *BLOCKING)).fetchone()


def _blocked(job_id: int, live) -> dict:
    return {"ok": False,
            "reason": f"job {job_id} already has a {live['status']} attempt"}


def classify_failure(reason: str, prior_failures: int) -> str:
    if reason in PERMANENT_REASONS:
        return "failed_permanent"
    return "failed_permanent" if prior_failures + 1 >= MAX_ATTEMPTS else "failed"


def is_unknown_state(reason: str) -> bool:
    return reason.split(":", 1)[0].strip() in UNKNOWN_STATE_REASONS


def sweep_stale_in_flight(conn: sqlite3.Connection, minutes: int = 30) -> int:
    """A crash during submission leaves in_flight behind. Its true state is
    unknown, so it blocks rather than allowing a possible double send.

    30 minutes, paired with agent.run_agent's 1200 s deadline and always the
    LARGER of the two: the agent's own deadline is armed at spawn, so a
    timing-out run always resolves its own row before this sweep can touch
    it, and the sweep-vs-returning-run race never opens. Raise one of the
    two and you must raise the other (tests/test_apply_ats.py
    ::test_the_agent_deadline_fires_before_the_sweep_window enforces it)."""
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


async def _live_run_agent(prompt: str, job_id: int, nonce: str):
    """Default agent runner: a real Chrome around a real `claude` session.
    Tests inject their own run_agent instead -- nothing in the test suite
    ever reaches this, by house convention (no test spawns a browser or a
    subprocess)."""
    from career_agent.apply import chrome as chrome_mod

    proc = chrome_mod.launch_chrome()
    try:
        return await agent_mod.run_agent(prompt, job_id=job_id, nonce=nonce)
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


# ponytail: one global lock; per-port locks when P2 adds parallel workers.
# chrome.py is a single-worker design -- one CDP port (9222), one profile
# dir, and _run_agent_blocking wipes the shared session dir and rewrites the
# shared .mcp-apply.json per run -- and launch_chrome _kill_port()s 9222
# before every launch. Without this, a dashboard Apply click during an
# auto-worker run taskkills the in-flight Chrome mid-submission. The design
# doc already says "single worker in P0"; this makes it true.
_AGENT_LOCK: asyncio.Lock | None = None
_AGENT_LOCK_LOOP = None


def _agent_lock() -> asyncio.Lock:
    """The one in-flight agent run. Rebound when the running event loop
    changes: asyncio.Lock binds itself to the first loop that awaits it and
    refuses any other, and the test suite gives every test its own loop.
    Production has exactly one loop (the FastAPI server's -- web/pipeline.py
    is the only code that runs a second one, and it never calls submit()),
    so the rebind never fires there."""
    global _AGENT_LOCK, _AGENT_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _AGENT_LOCK is None or _AGENT_LOCK_LOOP is not loop:
        _AGENT_LOCK, _AGENT_LOCK_LOOP = asyncio.Lock(), loop
    return _AGENT_LOCK


def _stage_resume(resume_path: Path, profile: CandidateProfile,
                  job_id: int) -> Path:
    """Copy the rendered résumé into the agent's work dir under a clean
    `<Candidate_Name>_Resume.docx` and return that path -- spec 3.2 FILES.

    The filename is the single most visible artefact of this whole system:
    handing the agent `tailored-847-r1.docx` tells every recruiter who
    opens the attachment that it was machine-generated per job. A file
    input uploads the basename, so only that changes; the bytes are copied
    verbatim and the stored file is never touched.

    Per-job subdirectory, because the basename is the same for every job:
    staging happens before `_agent_lock()` is taken, so one shared filename
    would let a second submit() overwrite the copy a first run is about to
    upload -- and send someone the wrong résumé."""
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_",
                  profile.candidate_name.strip()).strip("_") or "Candidate"
    dest = (agent_mod.WORK_DIR / f"job{job_id}"
            / f"{safe}_Resume{resume_path.suffix}").resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(resume_path, dest)
    return dest


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
        # ... but when BOTH are empty there is no audit trail at all for a
        # real submission: parse_result only demands ANSWERS_JSON for
        # DRAFT_READY, so auto mode's bare RESULT:APPLIED lands here with
        # `{}`. Record the send regardless -- it happened, and that is the
        # irreversible truth -- but never silently: failure_reason on a
        # 'submitted' row is the gap marker, not a failure.
        gap = "answers_json_missing" if not answers else None
        conn.execute(
            "UPDATE application SET status = 'submitted', answers = ?,"
            " submitted_at = datetime('now'), transcript_path = ?,"
            " failure_reason = ? WHERE id = ?",
            (json.dumps(answers), result.transcript_path or None, gap, app_id))
        conn.execute("INSERT INTO event (job_id, type, payload)"
                     " VALUES (?, 'submitted', ?)", (job_id, url))
        if gap:
            conn.execute("INSERT INTO event (job_id, type, payload)"
                         " VALUES (?, ?, ?)",
                         (job_id, gap, "submitted with no answers recorded:"
                          f" {result.transcript_path or url}"))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "submitted"}

    if code == "captcha":
        # `AND status = 'in_flight'` for the same reason as the failure path
        # below: a run can outlive sweep_stale_in_flight and come back to
        # find its own row already held_unknown. Deleting that row would
        # re-admit a job this run may already have submitted.
        conn.execute("DELETE FROM application WHERE id = ?"
                     " AND status = 'in_flight'", (app_id,))
        conn.execute("INSERT INTO event (job_id, type, payload)"
                     " VALUES (?, 'captcha_held', ?)", (job_id, url))
        conn.commit()
        return {"ok": False, "held": True,
                "reason": "captcha encountered; held for review"}

    if code == "needs_answer":
        # Nothing was sent, so the attempt leaves no trace: the worker
        # parks on the question and the job re-enters the queue once it is
        # answered.
        conn.execute("DELETE FROM application WHERE id = ?"
                     " AND status = 'in_flight'", (app_id,))   # same guard
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
    # `AND status = 'in_flight'` closes a double-submit race: the agent's
    # deadline is meant to fire well before sweep_stale_in_flight's window,
    # but if that kill ever fails to land a run can outlive the sweep and
    # come back to find its own row already held_unknown.
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


async def _run(runner, prompt: str, job_id: int, nonce: str) -> tuple:
    """run_agent does not return an AgentResult on every path -- a broken
    stdin pipe or a missing `claude`/`npx` binary raises out of it. Turn
    that into a recordable result so the caller always has one, and never
    leaves an in_flight row stranded.

    Returns (result, detail). The exception text goes in `detail`, for the
    event payload, so `failure_reason` stays the queryable taxonomy slug
    the schema promises (spec section 5.3)."""
    try:
        result = await runner(prompt, job_id, nonce)
        # The only place the run's price is recorded: AgentResult carries
        # cost_usd/duration_ms, there is no DB column for either (P0), and
        # the transcript footer is the other half of the record.
        log.info("apply agent job %s: %s ($%.4f, %d ms, transcript %s)",
                 job_id, result.code, result.cost_usd, result.duration_ms,
                 result.transcript_path or "none")
        return result, ""
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
    `async (prompt: str, job_id: int, nonce: str) -> AgentResult`."""
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

    live = _blocking_status(conn, job_id)
    if live:
        return _blocked(job_id, live)

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
                   _resume_text(resume_row),
                   str(_stage_resume(resume_path, profile, job_id)))
    score = score_row["weighted_score"] if score_row else None
    runner = run_agent or _live_run_agent
    # One unguessable token per run, stamped into every RESULT line the
    # prompt teaches and the only one parse_result will accept back -- a
    # job page cannot guess it, so it cannot forge an outcome. The runner
    # carries it through to parse_result; nothing else ever sees it.
    nonce = agent_mod.new_nonce()

    if dry_run:
        prompt = agent_mod.build_prompt(*prompt_args, mode="draft",
                                        nonce=nonce, score=score)
        async with _agent_lock():
            result, detail = await _run(runner, prompt, job_id, nonce)
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
        prompt = agent_mod.build_prompt(*prompt_args, mode="send", nonce=nonce,
                                        pinned_answers=pinned, score=score)
    else:
        prompt = agent_mod.build_prompt(*prompt_args, mode="auto", nonce=nonce,
                                        score=score)

    # The in_flight row is written INSIDE the lock: started_at is what
    # sweep_stale_in_flight measures, so a run queued behind another would
    # otherwise age toward that window while it had not begun.
    async with _agent_lock():
        # ...and the BLOCKING check is re-run here, authoritatively. The
        # early one above ran before the lock's await, so while a third run
        # holds the lock the auto worker and a dashboard Send can both pass
        # it seeing no live row; the loser's INSERT would then raise
        # sqlite3.IntegrityError (one_live_application_per_job) out of
        # submit(), which worker.apply_tick turns into run_state 'error' --
        # stopping the whole apply queue over a refusal it already knows how
        # to report.
        live = _blocking_status(conn, job_id)
        if live:
            return _blocked(job_id, live)
        cur = conn.execute(
            "INSERT INTO application (job_id, resume_version, status, started_at)"
            " VALUES (?, ?, 'in_flight', datetime('now'))",
            (job_id, resume_version))
        app_id = cur.lastrowid
        conn.commit()
        result, detail = await _run(runner, prompt, job_id, nonce)
    return _record_send_outcome(conn, job_id, app_id, row["url"], pinned,
                                result, detail)
