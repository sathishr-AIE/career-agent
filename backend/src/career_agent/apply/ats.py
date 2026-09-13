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

from career_agent import chat
from career_agent.apply import agent as agent_mod
from career_agent.apply.runner import RunEvents
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
    # A human's DECISION cancel: a choice about this job, not a failure to
    # retry. Re-queue it deliberately via queue_retry if they change their mind.
    "cancelled",
}

# Reasons that mean the agent drove a real browser and then stopped
# reporting: it may have clicked Submit before it died. When the run COULD
# submit (can_submit) these are neither "failed" nor "permanent" -- the
# application's true state is UNKNOWN, so they become 'held_unknown' (a
# BLOCKING status) and a human adjudicates, exactly as sweep_stale_in_flight
# does for a crashed in_flight row (see docs/lld-apply-button-v2.md section
# 6, layer 2). With submission disabled nothing could have been sent, so
# they stay retryable. `answer_timeout` is deliberately NOT here: the agent
# only waits at ASK/CONFIRM and CONFIRM precedes Submit.
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
    ::test_the_agent_deadline_fires_before_the_sweep_window enforces it).

    ...except that the work clock pauses while the agent waits on a human
    (run_session's answer_wait_s), so a healthy run can pass this window.
    Jobs with a live run in agent.RUNS are skipped: this process is still
    driving them, and their own watchdog resolves the row."""
    live = list(agent_mod.RUNS)
    cur = conn.execute(
        "UPDATE application SET status = 'held_unknown'"
        " WHERE status = 'in_flight'"
        f"  AND started_at < datetime('now', '-{int(minutes)} minutes')"
        f"  AND job_id NOT IN ({','.join('?' * len(live))})", live)
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


async def _live_run_agent(prompt: str, job_id: int, nonce: str,
                          events: RunEvents):
    """Default agent runner: a real Chrome around a real `claude` session.
    Tests inject their own run_agent instead -- nothing in the test suite
    ever reaches this, by house convention (no test spawns a browser or a
    subprocess)."""
    from career_agent.apply import chrome as chrome_mod

    proc = chrome_mod.launch_chrome()
    try:
        return await agent_mod.run_agent(prompt, job_id=job_id, nonce=nonce,
                                         events=events)
    except asyncio.CancelledError:
        _kill_live(job_id, events)   # before cleanup: never leave claude driving a dead Chrome
        raise
    finally:
        chrome_mod.cleanup(proc)


def _kill_live(job_id: int, events: RunEvents) -> None:
    """A cancelled await does not stop the to_thread worker: the claude
    session would keep running unobserved. Flag the run FIRST (run_session
    checks the flag after registering, so a run not yet registered never
    spawns), then kill it if it is registered -- only if it is this run."""
    events.cancelled.set()
    run = agent_mod.RUNS.get(job_id)
    if run is not None and getattr(run, "events", events) is events:
        run.kill()


# Tool calls worth a line in the chat; snapshots, waits, evaluates are noise.
_NARRATED_TOOLS = {"browser_navigate", "browser_file_upload", "browser_click",
                   "browser_fill_form"}


def _chat_events(conn_factory, job_id: int, nonce: str, mode: str = "manual",
                 prompt_baseline: int = 0) -> RunEvents:
    """RunEvents that narrate a run into the job's conversation, or no-ops
    when there is no conn_factory.

    The callbacks run on AgentRun's reader thread, and db.connect leaves
    sqlite3's check_same_thread on: the caller's connection raises there. So
    each event opens its own connection and closes it -- cheap at this
    message volume. A failed write is logged and dropped: narration must
    never stop an application mid-form.

    Protocol lines (RESULT/ASK/CONFIRM stamped with this run's nonce) are
    cut from the narration: they would put the nonce and raw JSON in the
    chat, and the ASK/CONFIRM cards reach it through on_ask/on_confirm.

    ASK and CONFIRM open an agent_prompt card. In auto mode a CONFIRM is
    approved on the spot (the prompt pre-approves it) and DECISION approve
    is sent if the run is waiting -- the ONLY approval not given by a human
    through actions.answer_prompt, and only when that approval is the one
    recorded: a human answer that landed first wins."""
    def with_conn(fn, what: str):
        if conn_factory is None:
            return None
        try:
            c = conn_factory()
            try:
                return fn(c)
            finally:
                c.close()
        except Exception:
            log.warning("could not record %s for job %s", what, job_id, exc_info=True)
            return None

    def post(role: str, content: str) -> None:
        with_conn(lambda c: chat.post_message(
            c, chat.conversation_for_job(c, job_id), role, content), f"a {role} message")

    def on_ask(payload: dict) -> None:
        with_conn(lambda c: chat.open_prompt(c, job_id, payload["kind"], payload), "an ASK")

    def on_confirm(payload: dict) -> None:
        def record(c):
            pid = chat.open_prompt(c, job_id, "confirm", payload)
            if mode != "auto" or chat.answer_prompt_row(c, pid, {"decision": "approve"}) is None:
                return False
            chat.post_message(c, chat.conversation_for_job(c, job_id), "system",
                              "Auto mode: application summary approved without review")
            return True
        if with_conn(record, "a CONFIRM"):
            run = agent_mod.RUNS.get(job_id)
            if run is not None:     # False when not waiting: the pre-approved agent went on
                run.send(agent_mod.answer_line(nonce, "confirm", {"decision": "approve"}))

    def on_tool(name: str, summary: str) -> None:
        if name in _NARRATED_TOOLS:
            post("system", f"{name} {summary}")

    protocol = (agent_mod.result_prefix(nonce), agent_mod.ask_prefix(nonce),
                agent_mod.confirm_prefix(nonce))

    def on_text(text: str) -> None:
        kept = "\n".join(l for l in text.splitlines()
                         if not agent_mod.strip_decoration(l).startswith(protocol)).strip()
        if kept:
            post("agent", kept)

    return RunEvents(on_text=on_text, on_tool=on_tool, on_ask=on_ask, on_confirm=on_confirm,
                     prompt_baseline=prompt_baseline)


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
# dir, and run_session wipes the shared session dir and rewrites the
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


def _confirm_outcome(conn, job_id: int, baseline: int) -> tuple[dict, str | None, bool]:
    """(answers, gap, approved_any) for this run's cards (ids > baseline).

    Answers are the fields of the run's LATEST CONFIRM, and only if that very
    card was approved: a later unapproved CONFIRM (a field changed after an
    approval) must never be recorded under the earlier approval. gap is
    `confirm_missing` for no CONFIRM at all, `unapproved` for a latest CONFIRM
    nobody approved. approved_any: any DECISION approve went out this run."""
    rows = conn.execute(
        "SELECT payload, status, json_extract(answer, '$.decision') AS decision"
        " FROM agent_prompt WHERE job_id = ? AND id > ? AND kind = 'confirm'"
        " ORDER BY id", (job_id, baseline)).fetchall()
    approved_any = any(r["status"] == "answered" and r["decision"] == "approve" for r in rows)
    if not rows:
        return {}, "confirm_missing", approved_any
    latest = rows[-1]
    if latest["status"] != "answered" or latest["decision"] != "approve":
        return {}, "unapproved", approved_any
    return ({f["label"]: f["value"] for f in json.loads(latest["payload"])["fields"]},
            None, approved_any)


def _record_outcome(conn, job_id: int, app_id: int, url: str, result,
                    detail: str, can_submit: bool, confirm: tuple) -> dict:
    """The one outcome recorder: turns this run's in_flight row into what
    happened. Every non-submitted UPDATE/DELETE is guarded on
    `status = 'in_flight'`: a run can outlive sweep_stale_in_flight and come
    back to find its row held_unknown, and downgrading that re-admits a job
    this run may already have submitted."""
    code = result.code
    answers, gap, approved_any = confirm
    event = lambda type_, payload: conn.execute(
        "INSERT INTO event (job_id, type, payload) VALUES (?, ?, ?)",
        (job_id, type_, payload))

    if code == "applied" and can_submit:
        # Recorded regardless of a missing/unapproved CONFIRM -- the send
        # happened, that is the irreversible truth -- but never silently:
        # failure_reason on a 'submitted' row is the gap marker. Unguarded on
        # purpose: held_unknown -> submitted is the truthful upgrade.
        slug = {"unapproved": "submitted_without_decision"}.get(gap, gap)
        conn.execute(
            "UPDATE application SET status = 'submitted', answers = ?,"
            " submitted_at = datetime('now'), transcript_path = ?,"
            " failure_reason = ? WHERE id = ?",
            (json.dumps(answers), result.transcript_path or None, slug, app_id))
        event("submitted", url)
        if slug:
            event(slug, f"submitted without an approved latest CONFIRM: {result.transcript_path or url}")
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "submitted"}

    if code == "draft_ready":
        conn.execute(
            "UPDATE application SET status = 'draft', answers = ?, transcript_path = ?"
            " WHERE id = ? AND status = 'in_flight'",
            (json.dumps(answers), result.transcript_path or None, app_id))
        if gap:
            slug = {"unapproved": "draft_without_decision"}.get(gap, gap)
            event(slug, f"draft without an approved latest CONFIRM: {result.transcript_path or url}")
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    if code in ("captcha", "needs_answer"):
        # Nothing was sent: the attempt leaves no row.
        conn.execute("DELETE FROM application WHERE id = ?"
                     " AND status = 'in_flight'", (app_id,))
        if code == "captcha":
            event("captcha_held", result.transcript_path or url)
            conn.commit()
            return {"ok": False, "held": True,
                    "reason": "captcha encountered; held for review"}
        conn.commit()
        # Never falsy: worker.apply_tick parks on `if result.get("needs_answer")`.
        question = result.reason or "(question not reported)"
        return {"ok": False, "needs_answer": question,
                "reason": f"needs an answer: {question}"}

    if code == "applied":
        # Submission was disabled, yet the agent says it clicked Submit: the
        # true state is unknown and possibly submitted -- hold, never retry.
        status, reason = "held_unknown", "applied_during_draft"
    else:
        reason = _reason_of(result)
        # Unknown-state reasons hold only when a submit was possible at all.
        # answer_timeout is retryable unless a DECISION approve went out in a
        # run that could submit: a post-submit questionnaire can ASK after the
        # Submit click, and requeueing that would apply twice.
        if can_submit and (is_unknown_state(reason)
                           or (reason == "answer_timeout" and approved_any)):
            status = "held_unknown"
        else:
            status = classify_failure(reason, _prior_failures(conn, job_id))
    conn.execute(
        "UPDATE application SET status = ?, failure_reason = ?,"
        " transcript_path = ? WHERE id = ? AND status = 'in_flight'",
        (status, reason, result.transcript_path or None, app_id))
    event(status, detail or reason)
    conn.commit()
    return {"ok": False, "reason": f"{status}: {detail or reason}"}


async def _run(runner, prompt: str, job_id: int, nonce: str,
               events: RunEvents) -> tuple:
    """run_agent does not return an AgentResult on every path -- a broken
    stdin pipe or a missing `claude`/`npx` binary raises out of it. Turn
    that into a recordable result so the caller always has one, and never
    leaves an in_flight row stranded.

    Returns (result, detail). The exception text goes in `detail`, for the
    event payload, so `failure_reason` stays the queryable taxonomy slug
    the schema promises (spec section 5.3)."""
    try:
        result = await runner(prompt, job_id, nonce, events)
        # The only place the run's price is recorded: AgentResult carries
        # cost_usd/duration_ms, there is no DB column for either (P0), and
        # the transcript footer is the other half of the record.
        log.info("apply agent job %s: %s (%s, %d ms, transcript %s)",
                 job_id, result.code,
                 agent_mod.cost_label(None if result.usage else result.cost_usd,
                                      result.usage),
                 result.duration_ms, result.transcript_path or "none")
        return result, ""
    except asyncio.CancelledError:
        _kill_live(job_id, events)
        raise
    except agent_mod.PreconditionError as exc:
        # submit()'s preflight normally catches this first; reaching here
        # means the backstop inside run_session fired. Nothing
        # launched, so it is a plain retryable failure -- never
        # 'agent_error', which would hold the row as possibly-submitted.
        log.warning("apply preconditions failed for job %s: %s", job_id, exc)
        return agent_mod.AgentResult("failed", "precondition"), str(exc)
    except Exception as exc:
        log.exception("apply agent crashed for job %s", job_id)
        return agent_mod.AgentResult("failed", "agent_error"), str(exc)


async def submit(conn: sqlite3.Connection, job_id: int, mode: str,
                 brief: CareerBrief,
                 profile: CandidateProfile | None = None,
                 resume_version: str | None = None,
                 run_agent=None, conn_factory=None) -> dict:
    """Run one apply-agent session for a job and translate its AgentResult
    into this module's state machine. `mode` is "manual" (every CONFIRM
    waits for the human's DECISION in the job chat) or "auto" (CONFIRMs are
    auto-approved). What an approval MEANS is the kill switch's call:
    can_submit = SUBMISSION_IMPLEMENTED or an injected run_agent (the test
    seam); without it the run still fills and CONFIRMs, never clicks Submit,
    and ends DRAFT_READY.

    `run_agent`: `async (prompt, job_id, nonce, events: RunEvents) ->
    AgentResult`. `conn_factory` is a zero-arg callable returning a NEW
    connection; with it narration, ASK and CONFIRM cards reach the job's
    conversation (see _chat_events), without it they are no-ops."""
    if mode not in ("manual", "auto"):
        raise ValueError(f"unknown mode {mode!r}")
    row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason": f"job {job_id} not found"}

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
    can_submit = SUBMISSION_IMPLEMENTED or run_agent is not None
    # One unguessable token per run, stamped into every protocol line the
    # prompt teaches and the only one the parsers accept back -- a job page
    # cannot guess it, so it cannot forge an outcome, a CONFIRM, or an ASK.
    nonce = agent_mod.new_nonce()
    prompt = agent_mod.build_prompt(*prompt_args, mode=mode, can_submit=can_submit,
                                    nonce=nonce, score=score)

    # The in_flight row is written INSIDE the lock, for every run: started_at
    # is what sweep_stale_in_flight measures, so a run queued behind another
    # would otherwise age toward that window while it had not begun.
    async with _agent_lock():
        # ...and the BLOCKING check is re-run here, authoritatively. The
        # early one above ran before the lock's await, so while a third run
        # holds the lock two same-job submits can both pass it seeing no live
        # row; the loser's INSERT would then raise sqlite3.IntegrityError
        # (one_live_application_per_job) out of submit().
        live = _blocking_status(conn, job_id)
        if live:
            return _blocked(job_id, live)
        # A crashed run's cards can never be answered; this run's cards are
        # the ones created after `baseline` (the answer API refuses older).
        chat.expire_open_prompts(conn, job_id)
        baseline = conn.execute("SELECT COALESCE(MAX(id), 0) m FROM agent_prompt").fetchone()["m"]
        events = _chat_events(conn_factory, job_id, nonce, mode, baseline)
        cur = conn.execute(
            "INSERT INTO application (job_id, resume_version, status, started_at)"
            " VALUES (?, ?, 'in_flight', datetime('now'))",
            (job_id, resume_version))
        app_id = cur.lastrowid
        conn.commit()
        result, detail = await _run(runner, prompt, job_id, nonce, events)
    # Outcome first, cards second: once the in_flight row is gone a refused
    # answer can no longer reopen a card (chat.reopen_prompt_row).
    outcome = _record_outcome(conn, job_id, app_id, row["url"], result, detail,
                              can_submit, _confirm_outcome(conn, job_id, baseline))
    chat.expire_open_prompts(conn, job_id)
    if outcome.get("needs_answer"):
        # The worker parks on this; the card is how the human unparks it
        # (actions.answer_prompt answers origin needs_answer with no live run).
        chat.open_prompt(conn, job_id, "text", {
            "id": "needs_answer", "kind": "text", "question": outcome["needs_answer"],
            "why": "The agent stopped to ask this before continuing.",
            "origin": "needs_answer", "memory_key": None, "default": None,
            "options": [], "sensitive": False})
    return outcome
