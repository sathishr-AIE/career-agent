"""The 'do something' half of the dashboard -- Apply, Send, Skip, and the
rest of the action routes. Every function here returns a plain
{"ok": bool, "message": str, ...} dict rather than an HTMLResponse, so the
Jinja routes (app.py) and the JSON routes (api.py) share one behavior and
can't drift: app.py wraps the dict in a <span>, api.py returns it as-is.

Messages are plain text, not HTML-escaped -- the Jinja side escapes at
render time (app.py's _span), so a message never gets double-escaped and
the JSON side gets clean text."""
import datetime as dt
import json
import logging
import os
import sqlite3
import tomllib
from pathlib import Path

import docx
from pydantic import ValidationError

from career_agent import chat, outcomes, store, tailor
from career_agent.apply import agent as agent_mod
from career_agent.apply import ats as ats_apply
from career_agent.apply import checkpoint
from career_agent.config import (SCORING_MODELS, CandidateProfile,
                                 CareerBrief, load_brief, save_brief,
                                 save_candidate_profile)
from career_agent.web import context, worker

log = logging.getLogger(__name__)


def _unpark(conn: sqlite3.Connection, job_id: int) -> None:
    """Release a run parked on this job. Manual mode leaves the run 'running'
    with current_job_id set to a drafted job awaiting review, and apply_tick
    returns early on every iteration while it is set -- so a run left parked
    on a job the user has already resolved never advances again."""
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


async def _do_apply(conn: sqlite3.Connection, job_id: int, allow_skip: bool,
                    event: str | None, brief_path: Path,
                    candidate_profile_path: Path, conn_factory=None) -> dict:
    parked = worker.get_run_state(conn, "apply")["current_job_id"]
    if parked is not None and parked != job_id:
        # Drafting job_id would set current_job_id to it below, silently
        # orphaning whatever's already parked -- its draft would still exist
        # but no card would ever point a Send button at it again.
        return {"ok": False, "message":
                "Another job is already parked awaiting review — resolve"
                " it first."}

    denial = worker.guard(conn, job_id, allow_skip=allow_skip, brief_path=brief_path)
    if denial:
        return {"ok": False, "message": denial}

    # Checked BEFORE the expiry below: a run already live on this job owns
    # its open cards, and expiring them would strand it waiting on an answer
    # that can no longer be given (submit() would refuse this apply anyway).
    live = ats_apply._blocking_status(conn, job_id)
    if live:
        return {"ok": False, "message": ats_apply._blocked(job_id, live)["reason"]}
    run = agent_mod.RUNS.get(job_id)
    if run is not None and not run.done.is_set():
        return {"ok": False, "message": f"job {job_id} already has a live agent run"}

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
    # Park on this job so the status card's Send/Skip review actually finds
    # the draft just written -- it only ever renders the job matching
    # current_job_id, same as apply_tick's own park-before-draft pattern.
    worker.set_run_state(conn, "apply", current_job_id=job_id)
    return {"ok": True, "message": "Applied"}


def answer_question(conn: sqlite3.Connection, job_id: int, question: str,
                    answer: str, is_volatile: bool) -> dict:
    # Legacy (Jinja) path. A live run's own ASK must be answered through the
    # run (answer_prompt); saving it here would unpark a job mid-run.
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


async def send(conn: sqlite3.Connection, job_id: int, brief_path: Path,
               candidate_profile_path: Path, conn_factory=None) -> dict:
    """Retired in S2: a live run's CONFIRM decision is the send. The route
    stays so an old page's button gets a clear answer, not a 404."""
    return {"ok": False, "message": "Answer the review card in the job's chat instead."}


_CLOSED = "That question is no longer open"
_NO_RUN = "No live agent run for this job"


def _refuse(code: int, message: str) -> dict:
    return {"ok": False, "code": code, "message": message}


def _checkpoint_answer(conn, job_id: int, prompt_id: int, kind: str, payload: dict,
                       body: dict) -> None:
    """What the run was told, for a resume's PREVIOUSLY ANSWERED. A sensitive
    answer is never written down; a CONFIRM pins only the human's changes.
    Never fails the answer: it already reached the agent."""
    if kind == "confirm":
        answers = body.get("changes", {})
    else:
        answers = {} if payload.get("sensitive") else {payload.get("question", kind): body["answer"]}
    try:
        checkpoint.mark_running(conn, job_id, f"answered {prompt_id}", answers)
    except Exception:
        log.warning("could not checkpoint answer %s for job %s", prompt_id, job_id, exc_info=True)


def answer_prompt(conn: sqlite3.Connection, prompt_id: int, answer: dict,
                  conn_factory=None) -> dict:
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
    if row["status"] != "open":
        return _refuse(409, _CLOSED)
    kind, payload = row["kind"], json.loads(row["payload"])
    if kind in ("need_password", "approve_account"):
        return _refuse(422, "Account actions arrive in a later slice.")
    answer = answer if isinstance(answer, dict) else {}

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
        if kind == "approve" and value not in ("approve", "reject"):
            return _refuse(422, "answer must be approve or reject")
        if kind == "text":
            value = value.strip()
        body = {"id": payload.get("id"), "answer": value,
                "remember": bool(answer.get("remember", False))}
        shown = "(hidden)" if payload.get("sensitive") else value
        summary = f"{payload.get('question', kind)} → {shown}"

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
    if chat.answer_prompt_row(conn, prompt_id, body) is None:
        return _refuse(409, _CLOSED)            # answered concurrently
    if not run.send(agent_mod.answer_line(run.nonce, kind, body)):
        chat.reopen_prompt_row(conn, prompt_id, run_ended=run.done.is_set())
        return _refuse(409, _NO_RUN)
    _checkpoint_answer(conn, row["job_id"], prompt_id, kind, payload, body)
    chat.post_message(conn, row["conversation_id"], "user", summary)
    return {"ok": True, "message": "Answer sent"}


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
