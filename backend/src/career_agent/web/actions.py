"""The 'do something' half of the dashboard -- Apply, Skip, and the
rest of the action routes. Every function here returns a plain
{"ok": bool, "message": str, ...} dict rather than an HTMLResponse, so the
Jinja routes (app.py) and the JSON routes (api.py) share one behavior and
can't drift: app.py wraps the dict in a <span>, api.py returns it as-is.

Messages are plain text, not HTML-escaped -- the Jinja side escapes at
render time (app.py's _span), so a message never gets double-escaped and
the JSON side gets clean text."""
import asyncio
import datetime as dt
import json
import logging
import os
import sqlite3
import tomllib
from pathlib import Path

import docx
from pydantic import ValidationError

from career_agent import chat, credentials, outcomes, store, tailor
from career_agent.apply import agent as agent_mod
from career_agent.apply import ats as ats_apply
from career_agent.apply import checkpoint, secret_fill
from career_agent.config import (SCORING_MODELS, CandidateProfile,
                                 CareerBrief, load_brief, save_brief,
                                 save_candidate_profile)
from career_agent.security import load_key
from career_agent.web import context, intent, pipeline, worker

log = logging.getLogger(__name__)


def _unpark(conn: sqlite3.Connection, job_id: int) -> None:
    """Release a run parked on this job. A needs_answer park leaves the run
    'running' with current_job_id set, and apply_tick returns early on every
    iteration while it is set -- so a run left parked on a job the user has
    already resolved never advances again."""
    if worker.get_run_state(conn, "apply")["current_job_id"] == job_id:
        worker.set_run_state(conn, "apply", current_job_id=None)


async def upload_master_resume(file) -> dict:
    """Install an uploaded DOCX as the tailoring master template.

    A resume people actually keep carries no <<SUMMARY>>/<<PROJECT_BULLET>>
    markers, so this prepares one: tailor.prepare_master reads the
    document's own structure and inserts them, and the response says
    exactly what it replaced. When the structure isn't legible it refuses
    rather than guessing at a paragraph.

    Everything happens on a temp copy and only lands via os.replace once it
    succeeds -- a master already installed must survive a bad upload
    untouched, and the user's own file is never modified at all."""
    target = tailor.TEMPLATE_PATH
    if not (file.filename or "").lower().endswith(".docx"):
        return {"ok": False, "message":
                "Only a .docx file can be the master template. Export from"
                " Word as .docx and try again."}

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".upload")
    try:
        tmp.write_bytes(await file.read())
        try:
            doc = docx.Document(str(tmp))
        except Exception:
            # python-docx on a PDF/renamed file raises an opaque zipfile
            # error; say what's actually wrong instead of leaking it.
            return {"ok": False, "message":
                    "That file could not be read as a Word document."
                    " Re-save it as .docx and try again."}
        if tailor.has_markers(doc):
            # Already prepared (or hand-marked): install the bytes as
            # uploaded. Re-saving through python-docx would rewrite the file
            # for no reason and make a re-upload non-idempotent.
            changes = []
        else:
            try:
                changes = tailor.prepare_master(doc)
            except ValueError as exc:
                return {"ok": False, "message":
                        f"Could not prepare this file: {exc}. Add the"
                        " <<SUMMARY>> and <<PROJECT_BULLET>> markers by hand"
                        " and re-upload.", "changes": []}
            doc.save(str(tmp))  # persist the markers just inserted
        os.replace(tmp, target)  # atomic; same directory keeps it a rename
    finally:
        Path(tmp).unlink(missing_ok=True)

    if not changes:
        return {"ok": True, "message":
                "Master resume installed as-is — it already had both"
                " markers. Apply will now tailor from it.", "changes": []}
    return {"ok": True, "message":
            "Master resume installed. Prepared it for tailoring. Everything"
            " else in the document was left untouched.", "changes": changes}


async def do_apply(conn: sqlite3.Connection, job_id: int, allow_skip: bool,
                   event: str | None, brief_path: Path,
                   candidate_profile_path: Path, conn_factory=None) -> dict:
    """The Applications page opens the job's chat before this returns, so a
    refusal is posted there too -- except the needs_answer park, whose card
    is already in the chat."""
    result = await _do_apply(conn, job_id, allow_skip, event, brief_path,
                             candidate_profile_path, conn_factory)
    if not result["ok"] and not result.pop("parked", False):
        worker.say(conn, job_id, f"Apply refused: {result['message']}")
    return result


def _apply_denial(conn: sqlite3.Connection, job_id: int, allow_skip: bool,
                  brief_path: Path) -> str | None:
    """Why Apply would refuse this job right now, or None."""
    parked = worker.get_run_state(conn, "apply")["current_job_id"]
    if parked is not None and parked != job_id:
        # The worker is on another job, or parked on its needs_answer question:
        # a needs_answer here would overwrite current_job_id and orphan that park.
        return _PARKED
    if job_id in ats_apply.PENDING:
        return _STARTING.format(job_id=job_id)

    denial = worker.guard(conn, job_id, allow_skip=allow_skip, brief_path=brief_path)
    if denial:
        return denial

    # Checked BEFORE _do_apply's expiry: a run already live on this job owns
    # its open cards, and expiring them would strand it waiting on an answer
    # that can no longer be given (submit() would refuse this apply anyway).
    live = ats_apply._blocking_status(conn, job_id)
    if live:
        return ats_apply._blocked(job_id, live)["reason"]
    run = agent_mod.RUNS.get(job_id)
    if run is not None and not run.done.is_set():
        return f"job {job_id} already has a live agent run"
    return None


async def _do_apply(conn: sqlite3.Connection, job_id: int, allow_skip: bool,
                    event: str | None, brief_path: Path,
                    candidate_profile_path: Path, conn_factory=None) -> dict:
    denial = _apply_denial(conn, job_id, allow_skip, brief_path)
    if denial:
        return {"ok": False, "message": denial}
    # Claimed before any await, released when the run returns: a double click, or
    # an Apply while the worker or a Continue tailors this job, is refused by
    # _apply_denial instead of queueing a second session.
    ats_apply.PENDING.add(job_id)
    try:
        return await _claimed_apply(conn, job_id, event, brief_path, candidate_profile_path,
                                    conn_factory)
    finally:
        ats_apply.PENDING.discard(job_id)


async def _claimed_apply(conn: sqlite3.Connection, job_id: int, event: str | None,
                         brief_path: Path, candidate_profile_path: Path,
                         conn_factory=None) -> dict:
    # Before any await: a stale needs_answer card answered while this job is
    # tailoring would unpark it and let the worker run it a second time.
    chat.expire_open_prompts(conn, job_id)

    if event:
        store.log(conn, job_id, event)

    try:
        resume_version = await worker.tailor_for_apply(conn, job_id, brief_path)
    except Exception as exc:
        return {"ok": False, "message": str(exc)}

    brief = load_brief(brief_path)
    profile = context.load_candidate_profile_or_none(candidate_profile_path)
    try:
        result = await ats_apply.submit(conn, job_id, mode="manual",
                                        brief=brief, profile=profile,
                                        resume_version=resume_version,
                                        conn_factory=conn_factory)
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    if result.get("needs_answer"):
        store.log(conn, job_id, "needs_answer", result["needs_answer"])
        # run_status_context only renders the "Answer needed" card when
        # current_job_id names this job -- apply_tick sets that as part of
        # its own needs_answer handling, but do_apply (the Apply/Override
        # button) never did, so the message below pointed at a card that
        # usually wasn't there unless the run happened to already be parked
        # on this exact job.
        worker.set_run_state(conn, "apply", current_job_id=job_id)
        return {"ok": False, "parked": True, "message":
                f'Answer needed: {result["needs_answer"]} — answer it in the'
                " job's chat."}
    if not result["ok"]:
        return {"ok": False, "message": result["reason"]}
    # No park: the in-session CONFIRM was the review (a draft blocks nothing).
    return {"ok": True, "message": "Applied"}


def answer_question(conn: sqlite3.Connection, job_id: int, question: str,
                    answer: str, is_volatile: bool) -> dict:
    # Legacy (Jinja) path. A live run's own ASK must be answered through the
    # run (answer_prompt); saving it here would unpark a job mid-run.
    if store.is_secret_card("text", {"question": question}):
        return {"ok": False, "message": _SECRET}
    run = agent_mod.RUNS.get(job_id)
    if run is not None and not run.done.is_set():
        return {"ok": False, "message":
                "The agent is still running this job — answer it in the job's chat."}
    open_row = chat.open_prompt_for_job(conn, job_id)
    if open_row and json.loads(open_row["payload"]).get("origin") != "needs_answer":
        return {"ok": False, "message":
                "The agent is waiting on this question live — answer it in the job's chat."}
    store.qa_upsert(conn, question, answer.strip(), is_volatile=is_volatile)
    if open_row:    # close the chat card too, or it stays answerable
        chat.answer_prompt_row(conn, open_row["id"], {"answer": answer.strip()})
    # Distinguishable from the needs_answer event that parked the job, so
    # run_status_context can tell "still needs an answer" apart from
    # "already answered, draft not back yet" -- see its comment for the
    # race this closes.
    store.log(conn, job_id, "needs_answer_resolved")
    _unpark(conn, job_id)
    return {"ok": True, "message": "Answer saved"}


_CLOSED = "That question is no longer open"
_EXPIRED = "That request expired — ask again"
_NO_RUN = "No live agent run for this job"
_SECRET = ("Secrets are never typed into chat — saved logins are filled by the backend;"
           " manage them in Logins.")
_PARKED = "Another job is running or parked on a question — resolve it first."
_STARTING = "job {job_id} already has an apply starting"


def _refuse(code: int, message: str) -> dict:
    return {"ok": False, "code": code, "message": message}


def _checkpoint_answer(conn, job_id: int, prompt_id: int, kind: str, payload: dict,
                       body: dict) -> None:
    """What the run was told, for a resume's PREVIOUSLY ANSWERED ("use verbatim"):
    only choice/text answers -- the ones memory keeps -- and a CONFIRM's changes.
    An approve, approve_account or need_password answer is never pinned, and a
    secret never reaches here. Never fails the answer: it already reached the agent."""
    if kind == "confirm":
        answers = body.get("changes", {})
    elif kind in ("choice", "text") and not store.is_secret_card(kind, payload):
        answers = {payload.get("question", kind): body["answer"]}
    else:
        answers = {}
    try:
        checkpoint.mark_running(conn, job_id, f"answered {prompt_id}", answers)
    except Exception:
        log.warning("could not checkpoint answer %s for job %s", prompt_id, job_id, exc_info=True)


class _NotSubmitted(Exception):
    """No empty password field on a real https page of the domain."""


_NOT_SUBMITTED_WHY = {
    "no_field": "no empty password field on an https {domain} page",
    "ambiguous_form": "the page shows more than one login form -- open just the sign-in "
                      "or sign-up form",
    "navigated": "the page changed during the fill",
    "no_submit": "the form did not submit",
    "value_persists": "the page kept the password in a field after it was cleared",
}


def _not_submitted(result: dict, domain: str) -> str:
    why = _NOT_SUBMITTED_WHY.get(result["reason"], result["reason"]).format(domain=domain)
    return f"{why}; the browser is on: {', '.join(result['pages']) or 'no pages'}"


def _uncleared(result: dict) -> str:
    return "" if result["cleared"] else "; the password field could not be cleared"


def _submit_saved_login(conn, run, payload: dict) -> tuple[dict, str]:
    """need_password: secret_fill types the saved password into the REAL page
    of the domain and submits the sign-in form; the agent continues from the
    resulting page. Its `url` is informational (an injected page can make it
    lie), and a stored row for a shared suffix is never used."""
    domain = payload.get("domain") or ""
    none = {"id": payload.get("id"), "answer": "none"}
    try:
        credentials.account_domain(domain)
    except ValueError:
        return none, f"Did not use a saved login for {domain}: not a site a login can belong to"
    cred = credentials.get(conn, domain)
    if cred is None:
        return none, f"No saved login for {domain}"
    run.secrets.add(cred["password"])       # defence in depth: scrubbed if it ever echoes
    result = secret_fill.fill_and_submit(domain, cred["password"], max_fields=1)
    if not result["submitted"]:
        return none, f"Did not use the saved login for {domain}: {_not_submitted(result, domain)}"
    return ({"id": payload.get("id"), "submitted": True},
            f"Submitted the saved login for {domain} on {result['page_url']}"
            + _uncleared(result))


def _create_login(conn, run, payload: dict, body: dict) -> tuple[dict, str]:
    """approve_account's approve: a generated password is filled into the
    sign-up form and submitted by secret_fill -- that submit is the Create
    click -- and stored right after the submit, before the field is cleared.
    ponytail: a sign-up the site rejects server-side still leaves the row
    (a failed creation looks like a success from here); the user deletes it
    in Logins."""
    domain = payload["domain"]
    pw = credentials.generate_password()
    load_key()                              # a key problem surfaces before anything is typed
    run.secrets.add(pw)                     # defence in depth: scrubbed if it ever echoes
    result = secret_fill.fill_and_submit(domain, pw, after_submit=lambda: credentials.put(
        conn, domain, payload["login_url"], payload["email"], pw, "agent"))
    if not result["submitted"]:
        raise _NotSubmitted(f"Did not create the login for {domain}: "
                            f"{_not_submitted(result, domain)}")
    return ({**body, "submitted": True},
            f"Saved login for {domain} (submitted on {result['page_url']})" + _uncleared(result))


def _account_refusal(conn, run, row, prompt_id: int, payload: dict) -> dict | None:
    """approve_account's checks before the claim. The domain must be one an
    account can be saved for, and the agent's login_url must be on it (the
    fill itself is checked against the REAL page). An existing login is never
    re-created: the agent is told "exists" so it signs in with it."""
    try:
        domain = credentials.account_domain(payload.get("domain") or "")
    except ValueError as exc:
        return _refuse(422, str(exc))
    if not credentials.host_matches(payload.get("login_url") or "", domain):
        shown = secret_fill.display_url(payload.get("login_url") or "") or "(none)"
        return _refuse(409, f"the sign-up page {shown} "
                            f"is not on {domain}")
    if not any(l["domain"] == domain for l in credentials.list_(conn)):
        return None
    exists = {"id": payload.get("id"), "answer": "exists"}
    if chat.answer_prompt_row(conn, prompt_id, exists) is None:
        return _refuse(409, _CLOSED)
    if not run.send(agent_mod.answer_line(run.nonce, "approve_account", exists)):
        chat.reopen_prompt_row(conn, prompt_id, run_ended=run.done.is_set())
        return _refuse(409, _NO_RUN)
    chat.post_message(conn, row["conversation_id"], "system",
                      f"A login already exists for {domain}; the agent will sign in with it")
    return _refuse(409, f"a login already exists for {domain}; use it, or delete it in "
                        "Logins first")


def answer_prompt(conn: sqlite3.Connection, prompt_id: int, answer: dict,
                  conn_factory=None, *, auto: bool = False,
                  brief_path: Path | None = None,
                  profile_path: Path | None = None, db_path: Path | None = None,
                  tasks: set | None = None, run_conn_factory=None) -> dict:
    """Answer one open ASK/CONFIRM card: validate it against the prompt's
    kind, record it, and write ANSWER:/DECISION: into the SAME live session.

    The live run is checked before anything is recorded, and a send the run
    refuses reopens the prompt: an answer the agent never received must not
    read as given (a DECISION approve especially -- it is what the outcome
    records as the reviewed answers). `code` is the HTTP status for api_chat."""
    row = conn.execute("SELECT * FROM agent_prompt WHERE id = ?",
                       (prompt_id,)).fetchone()
    if row is None:
        return _refuse(404, "No such question")
    if is_home_prompt(conn, row):
        return _answer_home_prompt(conn, row, answer, brief_path=brief_path,
                                   profile_path=profile_path, db_path=db_path,
                                   conn_factory=conn_factory, tasks=tasks,
                                   run_conn_factory=run_conn_factory)
    if row["status"] != "open":
        return _refuse(409, _CLOSED)
    kind, payload = row["kind"], json.loads(row["payload"])
    if kind == "need_password" and not auto:
        # Only the backend's own on-ASK path answers these (ats._chat_events,
        # auto=True): a human answer has nothing to add and must not fill.
        return _refuse(422, "The backend answers password requests itself")
    answer = answer if isinstance(answer, dict) else {}
    if store.is_secret_card(kind, payload):
        # Never relayed, never recorded. ats._chat_events opens no such card; this
        # covers one that got in another way, a needs_answer park included.
        return _refuse(422, _SECRET)

    if kind == "confirm":
        decision = answer.get("decision")
        if decision not in ("approve", "change", "cancel"):
            return _refuse(422, "decision must be approve, change, or cancel")
        body = {"decision": decision}
        if decision == "change":
            changes = answer.get("changes")
            if not isinstance(changes, dict) or not changes:
                return _refuse(422, "a change needs at least one changed field")
            body["changes"] = {str(k): str(v) for k, v in changes.items()}
            if any(not v.strip() for v in body["changes"].values()):
                return _refuse(422, "a changed field can't be blank")
            summary = "Change: " + "; ".join(f"{k} → {v}" for k, v in body["changes"].items())
        else:
            summary = {"approve": "Approved the application",
                       "cancel": "Cancelled this application"}[decision]
    else:
        value = answer.get("answer")
        if kind == "choice" and value not in payload.get("options", []):
            return _refuse(422, "Pick one of the offered options")
        if kind == "text" and not (isinstance(value, str) and value.strip()):
            return _refuse(422, "The answer can't be empty")
        if kind in ("approve", "approve_account") and value not in ("approve", "reject"):
            return _refuse(422, "answer must be approve or reject")
        if kind == "text":
            value = value.strip()
        body = {"id": payload.get("id"), "answer": value,
                "remember": bool(answer.get("remember", True))}
        shown = "(hidden)" if payload.get("sensitive") else value
        summary = f"{payload.get('question', kind)} → {shown}"
        if kind == "approve_account":
            body = {"id": payload.get("id"), "answer": value}
            summary = (f"{'Approved' if value == 'approve' else 'Rejected'} creating an "
                       f"account at {payload.get('domain')}")

    if payload.get("origin") == "needs_answer":
        # A parked job, not a live run: nothing to relay. The answer goes to
        # the qa bank the next attempt reads, and the park is released.
        if chat.answer_prompt_row(conn, prompt_id, body) is None:
            return _refuse(409, _CLOSED)
        store.qa_upsert(conn, payload["question"], value, is_volatile=False)
        store.log(conn, row["job_id"], "needs_answer_resolved")
        _unpark(conn, row["job_id"])
        chat.post_message(conn, row["conversation_id"], "user", summary)
        return {"ok": True, "message": "Answer saved"}

    run = agent_mod.RUNS.get(row["job_id"])
    if run is None or run.done.is_set() or not run.waiting.is_set():
        return _refuse(409, _NO_RUN)
    # The card must belong to THIS run and be the one it is waiting on: a
    # crashed run's leftover (or an older card) must never answer a later wait.
    newest = chat.open_prompt_for_job(conn, row["job_id"])
    if (prompt_id <= getattr(getattr(run, "events", None), "prompt_baseline", float("inf"))
            or newest is None or newest["id"] != prompt_id):
        return _refuse(409, _CLOSED)
    sent, notice = body, None   # what the agent gets; neither it nor `body` holds a password
    if kind == "approve_account" and value == "approve":
        refused = _account_refusal(conn, run, row, prompt_id, payload)
        if refused:
            return refused
    if kind == "need_password":
        body, summary = {"id": payload.get("id"), "answer": "by_backend"}, None
    if chat.answer_prompt_row(conn, prompt_id, body) is None:
        return _refuse(409, _CLOSED)            # answered concurrently
    # Claimed first: a double answer can't fill twice, or store one password and
    # submit another. A failure reopens the card; nothing is stored unsubmitted.
    try:
        if kind == "need_password":
            sent, notice = _submit_saved_login(conn, run, payload)
        elif kind == "approve_account" and value == "approve":
            sent, notice = _create_login(conn, run, payload, body)
    except _NotSubmitted as exc:
        chat.reopen_prompt_row(conn, prompt_id, run_ended=run.done.is_set())
        chat.post_message(conn, row["conversation_id"], "system", str(exc))
        return _refuse(409, str(exc))
    except Exception as exc:
        log.warning("%s fill/submit failed for prompt %s: %s", kind, prompt_id,
                    type(exc).__name__)
        chat.reopen_prompt_row(conn, prompt_id, run_ended=run.done.is_set())
        return _refuse(503, "Could not use the login in the browser; try again")
    if kind == "confirm" and decision == "approve":
        # Before the send: a crash just after it must never leave the session resumable.
        try:
            checkpoint.mark_approve_sent(conn, row["job_id"])
        except Exception:
            log.warning("could not checkpoint the approve for job %s", row["job_id"], exc_info=True)
    if not run.send(agent_mod.answer_line(run.nonce, kind, sent)):
        chat.reopen_prompt_row(conn, prompt_id, run_ended=run.done.is_set())
        return _refuse(409, _NO_RUN)
    _checkpoint_answer(conn, row["job_id"], prompt_id, kind, payload, body)
    # The answer is already sent and recorded at this point -- posting the
    # summary first means a memory-store hiccup below can never turn a
    # delivered answer into a failed response (see the two try/excepts).
    if summary:
        chat.post_message(conn, row["conversation_id"], "user", summary)
    if notice:                                  # never the password itself
        chat.post_message(conn, row["conversation_id"], "system", notice)
    # Remember a successfully-sent choice/text answer for next time, unless
    # the human opted out or the card was marked sensitive (never store a
    # password/SSN-shaped answer in qa_bank; qa_remember itself also
    # backstops this by content, in case a card isn't marked sensitive).
    # approve/confirm/need_password/approve_account are excluded by
    # construction: only "choice"/"text" reach here with `value` defined.
    if kind in ("choice", "text") and answer.get("remember", True) and not payload.get("sensitive"):
        try:
            store.qa_remember(conn, payload["question"], value, kind=kind,
                              options=payload.get("options") or None,
                              memory_key=payload.get("memory_key"),
                              source_job_id=row["job_id"], is_volatile=False)
        except Exception:
            log.exception("qa_remember failed for prompt %s -- answer was"
                          " already sent and recorded", prompt_id)
    if kind == "confirm" and decision == "approve":
        try:
            store.qa_touch(conn, payload.get("memory_used") or [])
        except Exception:
            log.exception("qa_touch failed for prompt %s -- answer was"
                          " already sent and recorded", prompt_id)
    return {"ok": True, "message": "Answer sent"}


# -- Home chat commands ---------------------------------------------------------

HELP_TEXT = ("I can find new jobs (\"find jobs\"), show your queue (\"what's my queue?\"),"
             " apply to a queued job (\"apply to #1639\" or \"apply to Acme\"),"
             " pause, resume or stop the apply run, and tell you the status.")
_ROUTER_DOWN = ("I couldn't work out what you meant — the command router may be unavailable"
                " (is the claude CLI installed and signed in?). Say \"help\" to see the commands. ")


def _background(coro, tasks: set | None) -> asyncio.Task:
    task = asyncio.create_task(coro)
    if tasks is not None:
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    return task


def pipeline_run_now(conn: sqlite3.Connection, conn_factory, db_path: Path, brief_path: Path,
                     tasks: set | None = None, run_conn_factory=None) -> dict:
    """Run Now (both frontends, and an approved Home "find jobs"). Must run on the loop."""
    if worker.get_run_state(conn, "pipeline")["status"] not in ("idle", "error"):
        return {"ok": False, "message": "A pipeline run is already in progress."}
    worker.set_run_state(conn, "pipeline", status="running", last_error=None,
                         stage=None, found=0, duplicates=0, passed=0,
                         scored=0, shortlisted=0)
    # Same as run_start()'s equivalent line for the apply kind: set_run_state
    # never touches started_at itself, and both the elapsed-time display and
    # the activity feed's run-scoping query (context.pipeline_status_context)
    # rely on it being real, not NULL.
    conn.execute("UPDATE run_state SET started_at = datetime('now') WHERE kind = 'pipeline'")
    conn.commit()
    store.log(conn, None, "pipeline_started")
    _background(pipeline.run_background(conn_factory, db_path, brief_path), tasks)
    return {"ok": True, "message": "ok"}


def _queue_text(conn) -> str:
    rows = conn.execute(
        "SELECT j.id, j.company, j.title FROM job j JOIN assessment a ON a.job_id = j.id"
        f" WHERE {worker.QUEUE_WHERE}"
        " ORDER BY j.priority ASC NULLS LAST, a.weighted_score DESC LIMIT 10").fetchall()
    if not rows:
        return "Your queue is empty."
    total = worker.queue_count(conn)
    lines = [f"#{r['id']} {r['company']} — {r['title']}" for r in rows]
    more = f"\n…and {total - len(rows)} more" if total > len(rows) else ""
    return f"{total} queued:\n" + "\n".join(lines) + more


def _status_text(conn) -> str:
    s = context.run_status_context(conn)
    state, stats, job = s["run_state"], s["stats"], s["current_job"]
    working = f", on #{job['job_id']} {job['company']} — {job['title']}" if job else ""
    return (f"Apply run: {state['status']} ({state['mode']} mode){working}."
            f" {stats['queued']} queued, {stats['total_applied']} applied,"
            f" {stats['failed_skipped']} failed or skipped."
            f" Discovery: {worker.get_run_state(conn, 'pipeline')['status']}."
            + (f" {stats['resumable']} interrupted — press Continue in each job's chat."
               if stats["resumable"] else ""))


def _ask_home(conn, action: str, args: dict, question: str) -> None:
    chat.open_home_prompt(conn, "approve", {"origin": "home", "kind": "approve",
                                            "action": action, "args": args, "question": question})


def _apply_to_reply(conn, job_ref: str | None) -> str | None:
    job_id = intent.resolve_job(conn, job_ref)
    if job_id is None:
        if not (job_ref or "").strip():
            return "Which job? Give me an id like #1639, or a company or title from your queue."
        matches = intent.describe_matches(conn, job_ref)
        if not matches:
            return f"I couldn't find a queued job matching \"{job_ref}\"."
        return ("That matches more than one job — which one? "
                + "; ".join(f"#{m['id']} {m['company']} — {m['title']}" for m in matches))
    job = conn.execute("SELECT company, title FROM job WHERE id = ?", (job_id,)).fetchone()
    # The id first, scraped text clipped: a title like "(#12)" must not pass for another job.
    clip = lambda s: s if len(s) <= 80 else s[:79] + "…"
    _ask_home(conn, "apply_to", {"job_id": job_id},
              f"Start applying to #{job_id}: {clip(job['title'])} at {clip(job['company'])}?")
    return None


async def home_message(conn: sqlite3.Connection, text: str, brief_path: Path, profile_path: Path,
                       conn_factory=None, *, runner=None, tasks: set | None = None) -> dict:
    """One Home chat message: record it, route it (claude, off the loop), and
    answer. Spend or submit intents only open an approve card; pause/resume/stop
    act directly (cheap, reversible). Never raises."""
    home = chat.home_conversation(conn)
    mid = chat.post_message(conn, home, "user", text)
    try:
        routed = await asyncio.to_thread(intent.route, text, runner=runner)
        name = routed["intent"]
        reply = None      # run controls narrate in Home themselves (worker.say)
        if name == "help":
            reply = HELP_TEXT
        elif name == "show_queue":
            reply = _queue_text(conn)
        elif name == "status":
            reply = _status_text(conn)
        elif name == "find_jobs":
            _ask_home(conn, "find_jobs", {}, "Run discovery now? (uses Apify + scoring credits)")
        elif name == "apply_to":
            reply = _apply_to_reply(conn, routed.get("job_ref"))
        elif name in ("pause_apply", "resume_apply"):
            # Free text never starts spending: resume only a paused run, pause only a running one.
            status = worker.get_run_state(conn, "apply")["status"]
            if name == "pause_apply" and status == "running":
                run_pause(conn)
            elif name == "resume_apply" and status == "paused":
                _background(_home_resume(conn, brief_path, profile_path, conn_factory), tasks)
            else:
                verb = "pause" if name == "pause_apply" else "resume"
                reply = f"Nothing to {verb} — the apply run is {status}."
        elif name == "stop_apply":
            run_stop(conn)
        elif routed.get("failed"):
            reply = _ROUTER_DOWN + HELP_TEXT
        else:
            reply = f"{routed['reply']} {HELP_TEXT}"
    except Exception:
        log.exception("home message failed")
        reply = f"Something went wrong handling that — the details are in the server log. {HELP_TEXT}"
    if reply:
        chat.post_message(conn, home, "agent", reply)
    return {"ok": True, "message_id": mid}


async def _home_resume(conn, brief_path, profile_path, conn_factory) -> None:
    try:
        await run_resume(conn, brief_path, profile_path, conn_factory)
    except Exception:
        log.exception("home resume failed")
        _home_says(conn, "Resuming the apply run failed — the details are in the server log.")


def is_home_prompt(conn, row) -> bool:
    """Only we open these (chat.open_home_prompt): an agent ASK loses its
    origin in parse_ask and lives in its job's conversation anyway."""
    return (json.loads(row["payload"]).get("origin") == "home"
            and row["conversation_id"] == chat.home_conversation(conn))


def _home_says(conn, text: str, payload: dict | None = None) -> None:
    chat.post_message(conn, chat.home_conversation(conn), "agent", text, payload)


def _home_find_jobs(conn, args: dict, *, conn_factory, db_path, brief_path, tasks,
                    run_conn_factory=None, **_) -> dict:
    result = pipeline_run_now(conn, run_conn_factory or conn_factory, db_path, brief_path, tasks)
    if not result["ok"]:
        _home_says(conn, result["message"])
        return _refuse(409, result["message"])
    _home_says(conn, "Discovery started — new matches land in your queue when it finishes.")
    return {"ok": True, "message": "Discovery started"}


def _home_apply_to(conn, args: dict, *, brief_path, profile_path, conn_factory, tasks, **_) -> dict:
    job_id = args.get("job_id")
    if type(job_id) is not int or not conn.execute("SELECT 1 FROM job WHERE id = ?",
                                                   (job_id,)).fetchone():
        text = f"Job #{job_id} no longer exists."
        _home_says(conn, text)
        return _refuse(409, text)
    verdict = conn.execute("SELECT verdict FROM assessment WHERE job_id = ?"
                           " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    if verdict and verdict["verdict"] == "skip":    # guard's own text points at a button Home lacks
        text = "The gate skipped this one — open the job's chat or the Jobs panel to override."
        _home_says(conn, text)
        return _refuse(409, text)
    # A readable refusal now; do_apply re-runs every guard when it starts.
    denial = _apply_denial(conn, job_id, False, brief_path)
    if denial:
        _home_says(conn, f"Apply refused: {denial}")
        return _refuse(409, denial)
    cid = chat.conversation_for_job(conn, job_id)
    _home_says(conn, f"Started — follow along in the job's chat (#{job_id})",
               {"job_id": job_id, "conversation_id": cid})
    _background(_home_apply_run(conn, job_id, brief_path, profile_path, conn_factory), tasks)
    return {"ok": True, "message": "Started", "job_id": job_id, "conversation_id": cid}


async def _home_apply_run(conn, job_id: int, brief_path, profile_path, conn_factory) -> None:
    try:
        result = await do_apply(conn, job_id, allow_skip=False, event="human_applied",
                                brief_path=brief_path, candidate_profile_path=profile_path,
                                conn_factory=conn_factory)
        text = result["message"]
    except Exception:
        log.exception("home apply failed for job %s", job_id)
        text = "Apply failed — the details are in the server log."
    _home_says(conn, f"#{job_id}: {text}")


HOME_ACTIONS = {"find_jobs": _home_find_jobs, "apply_to": _home_apply_to}


def _answer_home_prompt(conn, row, answer, **ctx) -> dict:
    """Approve/Reject a Home card: refuse a stale, superseded, or unknown one,
    claim it atomically, then dispatch its allowlisted action. Must run on the loop."""
    if row["status"] == "expired":
        return _refuse(409, _EXPIRED)
    if row["status"] != "open":
        return _refuse(409, _CLOSED)
    payload = json.loads(row["payload"])
    handler = HOME_ACTIONS.get(payload.get("action"))
    if row["kind"] != "approve" or handler is None:
        return _refuse(422, "That isn't something Home can do")
    decision = answer.get("answer") if isinstance(answer, dict) else None
    if decision not in ("approve", "reject"):
        return _refuse(422, "answer must be approve or reject")
    stale = conn.execute(
        "SELECT created_at < datetime('now', ?) OR EXISTS (SELECT 1 FROM agent_prompt n"
        "  WHERE n.conversation_id = p.conversation_id AND n.status = 'open' AND n.id > p.id)"
        " FROM agent_prompt p WHERE p.id = ?", (chat.HOME_PROMPT_TTL, row["id"])).fetchone()[0]
    if stale:
        conn.execute("UPDATE agent_prompt SET status = 'expired' WHERE id = ? AND status = 'open'",
                     (row["id"],))
        conn.commit()
        return _refuse(409, _EXPIRED)
    if chat.answer_prompt_row(conn, row["id"], {"answer": decision}) is None:
        return _refuse(409, _CLOSED)
    chat.post_message(conn, row["conversation_id"], "user",
                      f"{payload.get('question', 'Request')} → {decision}")
    if decision == "reject":
        _home_says(conn, "Cancelled")
        return {"ok": True, "message": "Cancelled"}
    return handler(conn, payload.get("args") or {}, **ctx)


def resume_job(conn: sqlite3.Connection, job_id: int, brief_path: Path,
               candidate_profile_path: Path, conn_factory=None, tasks: set | None = None) -> dict:
    """The job chat's "Continue where it left off". Refuses like do_apply (a
    BLOCKING attempt, a live run, the apply lock) and without a resumable
    checkpoint; otherwise the resumed run goes to the background -- it can wait
    on cards for many minutes -- tracked in `tasks` (app._background_tasks).
    The resume cap and a mode mismatch are submit()'s call; both reach the chat.
    Must be called on the event loop. `code` is the HTTP status for api_chat."""
    cp = checkpoint.get(conn, job_id)
    if cp is None or cp["status"] != "resumable":
        return _refuse(409, "Nothing to continue: this job has no interrupted session")
    live = ats_apply._blocking_status(conn, job_id)
    if live:
        return _refuse(409, ats_apply._blocked(job_id, live)["reason"])
    run = agent_mod.RUNS.get(job_id)
    if run is not None and not run.done.is_set():
        return _refuse(409, f"job {job_id} already has a live agent run")
    if ats_apply._agent_lock().locked():
        return _refuse(409, "Another application is running — continue when it ends")
    if job_id in ats_apply.PENDING:
        return _refuse(409, _STARTING.format(job_id=job_id))
    parked = worker.get_run_state(conn, "apply")["current_job_id"]
    if parked is not None and parked != job_id:
        return _refuse(409, _PARKED)
    denial = worker.guard(conn, job_id, allow_skip=True, brief_path=brief_path)
    if denial:
        return _refuse(409, denial)
    # Claimed until the run returns: the auto worker must not resume it meanwhile.
    checkpoint.set_auto_resumed(conn, job_id, True)
    after = chat.post_message(conn, chat.conversation_for_job(conn, job_id), "system",
                              "Continuing where it left off")
    ats_apply.PENDING.add(job_id)       # released by _holding when the run returns
    task = asyncio.create_task(_holding(job_id, _resume_run(
        conn, job_id, brief_path, candidate_profile_path, conn_factory)))
    if tasks is not None:
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    # `after`: every outcome of the background run posts a newer chat line.
    return {"ok": True, "message": "Continuing where it left off", "after": after}


async def _holding(job_id: int, coro) -> None:
    try:
        await coro
    finally:
        ats_apply.PENDING.discard(job_id)


async def _resume_run(conn, job_id: int, brief_path: Path, candidate_profile_path: Path,
                      conn_factory=None) -> None:
    """Manual mode, like do_apply: an auto session is restarted fresh by submit()."""
    try:
        resume_version = await worker.tailor_for_apply(conn, job_id, brief_path)
        result = await ats_apply.submit(
            conn, job_id, mode="manual", brief=load_brief(brief_path),
            profile=context.load_candidate_profile_or_none(candidate_profile_path),
            resume_version=resume_version, conn_factory=conn_factory, resume=True)
    except Exception as exc:
        log.exception("continue failed for job %s", job_id)
        worker.say(conn, job_id, f"Continue failed: {exc}")
        return
    finally:
        try:        # a human touch re-arms the worker's one auto-resume
            checkpoint.release_claim(conn, job_id)
        except Exception:
            log.warning("could not re-arm auto-resume for job %s", job_id, exc_info=True)
    worker.say(conn, job_id, worker._outcome_text(conn, job_id, result))


def dismiss(conn: sqlite3.Connection, job_id: int) -> dict:
    store.log(conn, job_id, "human_dismissed")
    return {"ok": True, "message": "Dismissed"}


def _parse_date(raw: str, field: str, errors: dict) -> str | None:
    """Accept an ISO date, defaulting to today when blank. Parsed here, not
    declared as a typed Form parameter: a typed parameter makes FastAPI
    reject a bad value with a raw 422 before this handler runs, which skips
    the friendly error page entirely."""
    raw = (raw or "").strip()
    if not raw:
        return context.utc_today().isoformat()
    try:
        return dt.date.fromisoformat(raw).isoformat()
    except ValueError:
        errors[field] = "must be a date like 2026-08-20"
        return None


def mark_applied(conn: sqlite3.Connection, job_id: int, when: str,
                 brief_path: Path) -> dict:
    errors: dict[str, str] = {}
    day = _parse_date(when, "when", errors)
    if errors:
        return {"ok": False, "message": errors["when"]}

    try:
        store.mark_applied(conn, job_id, day)
    except sqlite3.IntegrityError:
        return {"ok": False, "message":
                "This job already has a live application."}
    checkpoint.finish(conn, job_id)     # a human decision supersedes an interrupted session
    _unpark(conn, job_id)

    # A manual application counts against daily_cap like any other (see
    # worker.guard), and reaching it auto-pauses the apply run. Say so here,
    # or the pause looks unrelated to the click that caused it.
    cap = load_brief(brief_path).daily_cap
    today_submitted = context.today_submitted(conn)
    note = f" — daily cap of {cap} reached" if today_submitted >= cap else ""
    return {"ok": True, "message": f"Marked applied{note}"}


def record_outcome(conn: sqlite3.Connection, application_id: int, type: str,
                   occurred_at: str, notes: str) -> dict:
    row = conn.execute("SELECT status FROM application WHERE id = ?",
                       (application_id,)).fetchone()
    if row is None or row["status"] != "submitted":
        return {"ok": False, "message":
                "This application is not submitted yet. Mark the job"
                " applied before recording what came back."}

    errors: dict[str, str] = {}
    day = _parse_date(occurred_at, "occurred_at", errors)
    if errors:
        return {"ok": False, "message": errors["occurred_at"]}

    try:
        outcomes.record(conn, application_id, type, day,
                        notes=notes.strip() or None)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": "Recorded"}


async def queue_skip(conn: sqlite3.Connection, job_id: int, brief_path: Path,
                     candidate_profile_path: Path, conn_factory=None) -> dict:
    run = agent_mod.RUNS.get(job_id)
    if job_id in ats_apply.PENDING or (run is not None and not run.done.is_set()):
        # Clearing current_job_id under a live run would let the worker start
        # another job beside it.
        return {"ok": False, "message":
                "This job's agent run is still live — finish or cancel it in the job's chat."}
    store.log(conn, job_id, "job_skipped", "skipped by user")
    state = worker.get_run_state(conn, "apply")
    if state["current_job_id"] == job_id:
        worker.set_run_state(conn, "apply", current_job_id=None)
        nxt = worker.next_candidate(conn)
        if nxt is not None and nxt["job_id"] != job_id:
            await worker.apply_tick(conn, brief_path, candidate_profile_path,
                                    conn_factory)
    return {"ok": True, "message": "ok"}


def queue_retry(conn: sqlite3.Connection, job_id: int,
                confirm_not_submitted: bool = False) -> dict:
    """Requeue a job whose last attempt did not go through.

    `held_unknown` needs `confirm_not_submitted` because it means exactly
    "the agent drove a real browser and then stopped reporting": it may
    already have submitted, so releasing it on a plain retry click would be
    a double-submit vector. With the human's confirmation the row becomes a
    plain `failed` -- non-BLOCKING, so QUEUE_WHERE re-admits the job --
    keeping `failure_reason` so why it was held survives. The other exit,
    "it WAS submitted", is store.mark_applied, which promotes the same row.
    Before this, neither worked and raw SQL was the only way out of a state
    the applications page tells the user to clear."""
    app_row = conn.execute(
        "SELECT id, status, failure_reason FROM application WHERE job_id = ?"
        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    status = app_row["status"] if app_row else None
    if status == "held_unknown":
        if not confirm_not_submitted:
            return {"ok": False, "message":
                    "This one is held because the agent stopped reporting"
                    " mid-run — it may already have been submitted. Check the"
                    " employer's site first, then confirm it was not"
                    " submitted to clear the hold."}
        conn.execute("UPDATE application SET status = 'failed'"
                     " WHERE id = ? AND status = 'held_unknown'",
                     (app_row["id"],))
        conn.commit()
        store.log(conn, job_id, "hold_cleared",
                  app_row["failure_reason"] or "")
        _unpark(conn, job_id)
    elif status != "failed":
        return {"ok": False, "message":
                "Only a failed application can be retried."}
    lowest = conn.execute(
        "SELECT MIN(priority) p FROM job").fetchone()["p"]
    new_priority = (lowest - 1) if lowest is not None else 0
    conn.execute("UPDATE job SET priority = ? WHERE id = ?",
                 (new_priority, job_id))
    conn.commit()
    store.log(conn, job_id, "job_skipped", "retry requested; requeued")
    # A resumable checkpoint keeps the job out of QUEUE_WHERE: the retry supersedes it.
    checkpoint.finish(conn, job_id)
    return {"ok": True, "message": "Requeued"}


def _split_list(raw: str) -> list[str]:
    """List fields are comma-separated text, which keeps the Jinja form
    static HTML with no JS array widgets, and keeps the JSON body a flat
    string too -- one parsing rule for both."""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_int(raw: str, field: str, errors: dict) -> int | None:
    """Numeric settings fields arrive as text from both callers (Form
    fields on the Jinja side, JSON string values on the API side) and are
    parsed here rather than declared as typed fields -- a typed field lets
    its framework 422 the request before this function runs, bypassing the
    'nothing was saved' story and losing every other field submitted."""
    if not raw.strip():
        errors[field] = "is required"
        return None
    try:
        return int(raw)
    except ValueError:
        errors[field] = "must be a whole number"
        return None


def _validate_brief(form: dict, daily_cap: int, gate_threshold: int,
                    staleness_days: int, errors: dict) -> CareerBrief | None:
    """Build a CareerBrief from the form, recording any problems in `errors`.
    Validation is the pydantic model's job -- min_length on target_titles and
    search_locations, daily_cap >= 1, 0 <= gate_threshold <= 100,
    staleness_days >= 1 -- so no second rule set is written here."""
    try:
        return CareerBrief(
            target_titles=_split_list(form["target_titles"]),
            title_families=_split_list(form["title_families"]),
            search_locations=_split_list(form["search_locations"]),
            locations=_split_list(form["locations"]),
            work_authorization=_split_list(form["work_authorization"]),
            excluded_companies=_split_list(form["excluded_companies"]),
            non_negotiables=_split_list(form["non_negotiables"]),
            remote_ok=bool(form.get("remote_ok")),
            salary_floor_inr=(int(form["salary_floor_inr"])
                              if form["salary_floor_inr"].strip() else None),
            daily_cap=daily_cap, gate_threshold=gate_threshold,
            staleness_days=staleness_days)
    except ValidationError as exc:
        for err in exc.errors():
            field = err["loc"][0] if err["loc"] else "form"
            errors[str(field)] = err["msg"]
    except ValueError:
        errors["salary_floor_inr"] = "must be a whole number, or blank"
    return None


def save_settings(conn: sqlite3.Connection, form: dict, brief_path: Path,
                  candidate_path: Path) -> dict:
    """Validate and persist the whole Settings page -- Career Brief, Agent
    Settings, and Candidate Profile -- from one flat dict of string fields.
    Both the Jinja settings_save route and the JSON PUT /api/settings route
    build that dict from their own request shape and call this; it's the one
    place the three-section, validate-everything-before-writing-anything
    rule lives.

    `form` keys: target_titles, title_families, search_locations, locations,
    work_authorization, excluded_companies, non_negotiables, remote_ok
    (truthy/blank), salary_floor_inr, daily_cap, gate_threshold,
    staleness_days, scoring_model, max_score_per_run, brief_present
    (truthy/blank), candidate_present (truthy/blank), candidate_name,
    candidate_email, candidate_phone, linkedin_url, portfolio_url."""
    errors: dict[str, str] = {}

    # max_score_per_run's control is on the page unconditionally, so it's
    # always required.
    max_score_per_run_n = _parse_int(
        form["max_score_per_run"], "max_score_per_run", errors)

    # The brief numerics are required only when brief_present says that
    # section was actually submitted -- when the TOML can't be read, that
    # section is hidden and carries none of its fields, so demanding three
    # numbers here would break "Agent Settings still save" on a broken brief.
    daily_cap_n = gate_threshold_n = staleness_days_n = None
    if form.get("brief_present"):
        daily_cap_n = _parse_int(form["daily_cap"], "daily_cap", errors)
        gate_threshold_n = _parse_int(form["gate_threshold"], "gate_threshold", errors)
        staleness_days_n = _parse_int(form["staleness_days"], "staleness_days", errors)

    # Validate EVERYTHING before writing ANYTHING: a partial save would leave
    # the DB describing a state the TOML does not.
    brief = None
    if form.get("brief_present") and None not in (
            daily_cap_n, gate_threshold_n, staleness_days_n):
        brief = _validate_brief(form, daily_cap_n, gate_threshold_n,
                                staleness_days_n, errors)

    # candidate_present posts truthy even on a fresh install missing
    # candidate_profile.toml (it's gitignored) -- building a CandidateProfile
    # from all-blank fields would raise ValidationError and, since
    # everything validates before anything writes, that would block saving
    # the Career Brief and Agent Settings too, even though the user never
    # touched the candidate fields. Only attempt the build when the user put
    # something in at least one candidate field.
    candidate = None
    candidate_fields = (form.get("candidate_name", ""), form.get("candidate_email", ""),
                       form.get("candidate_phone", ""), form.get("linkedin_url", ""),
                       form.get("portfolio_url", ""))
    if form.get("candidate_present") and any(candidate_fields):
        # The form carries only the five flat fields. Merge them onto the
        # saved file's raw values so a Settings save never resets gender,
        # address, work_history or education to their defaults. Raw TOML,
        # not load_candidate_profile: an invalid flat field on disk must
        # stay fixable from this form.
        existing = {}
        if candidate_path.exists():
            with open(candidate_path, "rb") as f:
                existing = tomllib.load(f)
        try:
            candidate = CandidateProfile(**{
                **existing,
                "candidate_name": form.get("candidate_name", ""),
                "candidate_email": form.get("candidate_email", ""),
                "candidate_phone": form.get("candidate_phone", ""),
                "linkedin_url": form.get("linkedin_url") or None,
                "portfolio_url": form.get("portfolio_url") or None})
        except ValidationError as exc:
            for err in exc.errors():
                field = err["loc"][0] if err["loc"] else "form"
                errors[str(field)] = err["msg"]

    scoring_model = form.get("scoring_model", "")
    if scoring_model not in SCORING_MODELS:
        errors["scoring_model"] = f"unknown scoring model: {scoring_model}"
    if max_score_per_run_n is not None and max_score_per_run_n < 0:
        errors["max_score_per_run"] = "cannot be negative"

    if errors:
        return {"ok": False, "errors": errors}

    if brief is not None:
        save_brief(brief_path, brief)
    if candidate is not None:
        save_candidate_profile(candidate_path, candidate)
    store.save_settings(conn, scoring_model, max_score_per_run_n)
    return {"ok": True, "errors": {}}


async def run_start(conn: sqlite3.Connection, mode: str, brief_path: Path,
                    candidate_profile_path: Path, conn_factory=None) -> dict:
    worker.set_run_state(conn, "apply", status="running", mode=mode,
                         last_error=None)
    conn.execute("UPDATE run_state SET started_at = datetime('now')"
                 " WHERE kind = 'apply'")
    conn.commit()
    store.log(conn, None, "run_started", mode)
    worker.say(conn, None, f"Apply run started in {mode} mode")
    await worker.apply_tick(conn, brief_path, candidate_profile_path,
                            conn_factory)
    return {"ok": True, "message": "ok"}


def run_pause(conn: sqlite3.Connection) -> dict:
    worker.set_run_state(conn, "apply", status="paused")
    store.log(conn, None, "run_paused")
    worker.say(conn, None, "Apply run paused")
    return {"ok": True, "message": "ok"}


async def run_resume(conn: sqlite3.Connection, brief_path: Path,
                     candidate_profile_path: Path, conn_factory=None) -> dict:
    worker.set_run_state(conn, "apply", status="running")
    store.log(conn, None, "run_resumed")
    worker.say(conn, None, "Apply run resumed")
    await worker.apply_tick(conn, brief_path, candidate_profile_path,
                            conn_factory)
    return {"ok": True, "message": "ok"}


def run_stop(conn: sqlite3.Connection) -> dict:
    worker.set_run_state(conn, "apply", status="stopped", current_job_id=None)
    store.log(conn, None, "run_stopped")
    worker.say(conn, None, "Apply run stopped")
    return {"ok": True, "message": "ok"}


def queue_priority(conn: sqlite3.Connection, job_id: int, direction: str) -> dict:
    order = conn.execute(
        "SELECT j.id AS id, j.priority AS priority FROM job j"
        " JOIN assessment a ON a.job_id = j.id"
        f" WHERE {worker.QUEUE_WHERE}"
        " ORDER BY j.priority ASC NULLS LAST, j.id ASC").fetchall()
    ids = [r["id"] for r in order]
    if job_id not in ids:
        return {"ok": False, "message": "Not in the queue."}
    pos = ids.index(job_id)
    neighbor_pos = pos - 1 if direction == "up" else pos + 1
    if not (0 <= neighbor_pos < len(ids)):
        return {"ok": True, "message": "ok"}  # already at the edge; nothing to swap
    for i, row in enumerate(order):
        conn.execute("UPDATE job SET priority = ? WHERE id = ?", (i, row["id"]))
    conn.commit()
    a, b = ids[pos], ids[neighbor_pos]
    pa = conn.execute("SELECT priority FROM job WHERE id = ?", (a,)).fetchone()["priority"]
    pb = conn.execute("SELECT priority FROM job WHERE id = ?", (b,)).fetchone()["priority"]
    conn.execute("UPDATE job SET priority = ? WHERE id = ?", (pb, a))
    conn.execute("UPDATE job SET priority = ? WHERE id = ?", (pa, b))
    conn.commit()
    return {"ok": True, "message": "ok"}
