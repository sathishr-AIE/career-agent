"""The 'do something' half of the dashboard -- Apply, Send, Skip, and the
rest of the action routes. Every function here returns a plain
{"ok": bool, "message": str, ...} dict rather than an HTMLResponse, so the
Jinja routes (app.py) and the JSON routes (api.py) share one behavior and
can't drift: app.py wraps the dict in a <span>, api.py returns it as-is.

Messages are plain text, not HTML-escaped -- the Jinja side escapes at
render time (app.py's _span), so a message never gets double-escaped and
the JSON side gets clean text."""
import datetime as dt
import os
import sqlite3
from pathlib import Path

import docx
from pydantic import ValidationError

from career_agent import outcomes, store, tailor
from career_agent.apply import ats as ats_apply
from career_agent.config import (SCORING_MODELS, CandidateProfile,
                                 CareerBrief, load_brief, save_brief,
                                 save_candidate_profile)
from career_agent.web import context, worker


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
                   candidate_profile_path: Path) -> dict:
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

    if event:
        store.log(conn, job_id, event)

    try:
        resume_version = await worker.tailor_for_apply(conn, job_id, brief_path)
    except Exception as exc:
        return {"ok": False, "message": str(exc)}

    brief = load_brief(brief_path)
    profile = context.load_candidate_profile_or_none(candidate_profile_path)
    try:
        result = await ats_apply.submit(conn, job_id, dry_run=True,
                                        brief=brief, profile=profile,
                                        resume_version=resume_version)
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
        return {"ok": False, "message":
                f'Answer needed: {result["needs_answer"]} — see the status'
                ' card below.'}
    if not result["ok"]:
        return {"ok": False, "message": result["reason"]}
    # Park on this job so the status card's Send/Skip review actually finds
    # the draft just written -- it only ever renders the job matching
    # current_job_id, same as apply_tick's own park-before-draft pattern.
    worker.set_run_state(conn, "apply", current_job_id=job_id)
    return {"ok": True, "message": "Applied"}


def answer_question(conn: sqlite3.Connection, job_id: int, question: str,
                    answer: str, is_volatile: bool) -> dict:
    store.qa_upsert(conn, question, answer.strip(), is_volatile=is_volatile)
    # Distinguishable from the needs_answer event that parked the job, so
    # run_status_context can tell "still needs an answer" apart from
    # "already answered, draft not back yet" -- see its comment for the
    # race this closes.
    store.log(conn, job_id, "needs_answer_resolved")
    _unpark(conn, job_id)
    return {"ok": True, "message": "Answer saved"}


async def send(conn: sqlite3.Connection, job_id: int, brief_path: Path,
               candidate_profile_path: Path) -> dict:
    draft = conn.execute(
        "SELECT id FROM application WHERE job_id = ? AND status = 'draft'"
        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    if draft is None:
        return {"ok": False, "message": "No draft to send yet. Click Apply first."}

    denial = worker.guard(conn, job_id, allow_skip=True, brief_path=brief_path)
    if denial:
        return {"ok": False, "message": denial}

    store.log(conn, job_id, "human_confirmed_send")

    brief = load_brief(brief_path)
    profile = context.load_candidate_profile_or_none(candidate_profile_path)
    try:
        result = await ats_apply.submit(
            conn, job_id, dry_run=False, brief=brief, profile=profile,
            resume_version=store.resume_version_for(conn, job_id))
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    if not result["ok"]:
        # A categorical refusal -- submission is not implemented, which is
        # every real send today -- can never succeed on a retry, so the only
        # way forward is applying on the site: stop parking the run on it. A
        # transient failure (a captcha hold, a filler that errored) stays
        # parked, because that draft is still the thing to retry and the
        # status card should keep pointing at it.
        if result.get("unsupported"):
            _unpark(conn, job_id)
        return {"ok": False, "message": result["reason"]}
    _unpark(conn, job_id)
    return {"ok": True, "message": "Sent"}


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
                     candidate_profile_path: Path) -> dict:
    store.log(conn, job_id, "job_skipped", "skipped by user")
    state = worker.get_run_state(conn, "apply")
    if state["current_job_id"] == job_id:
        worker.set_run_state(conn, "apply", current_job_id=None)
        nxt = worker.next_candidate(conn)
        if nxt is not None and nxt["job_id"] != job_id:
            await worker.apply_tick(conn, brief_path, candidate_profile_path)
    return {"ok": True, "message": "ok"}


def queue_retry(conn: sqlite3.Connection, job_id: int) -> dict:
    app_row = conn.execute(
        "SELECT status FROM application WHERE job_id = ?"
        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    if app_row is None or app_row["status"] != "failed":
        return {"ok": False, "message":
                "Only a failed application can be retried."}
    lowest = conn.execute(
        "SELECT MIN(priority) p FROM job").fetchone()["p"]
    new_priority = (lowest - 1) if lowest is not None else 0
    conn.execute("UPDATE job SET priority = ? WHERE id = ?",
                 (new_priority, job_id))
    conn.commit()
    store.log(conn, job_id, "job_skipped", "retry requested; requeued")
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
        try:
            candidate = CandidateProfile(
                candidate_name=form.get("candidate_name", ""),
                candidate_email=form.get("candidate_email", ""),
                candidate_phone=form.get("candidate_phone", ""),
                linkedin_url=form.get("linkedin_url") or None,
                portfolio_url=form.get("portfolio_url") or None)
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
                    candidate_profile_path: Path) -> dict:
    worker.set_run_state(conn, "apply", status="running", mode=mode,
                         last_error=None)
    conn.execute("UPDATE run_state SET started_at = datetime('now')"
                 " WHERE kind = 'apply'")
    conn.commit()
    store.log(conn, None, "run_started", mode)
    await worker.apply_tick(conn, brief_path, candidate_profile_path)
    return {"ok": True, "message": "ok"}


def run_pause(conn: sqlite3.Connection) -> dict:
    worker.set_run_state(conn, "apply", status="paused")
    store.log(conn, None, "run_paused")
    return {"ok": True, "message": "ok"}


async def run_resume(conn: sqlite3.Connection, brief_path: Path,
                     candidate_profile_path: Path) -> dict:
    worker.set_run_state(conn, "apply", status="running")
    store.log(conn, None, "run_resumed")
    await worker.apply_tick(conn, brief_path, candidate_profile_path)
    return {"ok": True, "message": "ok"}


def run_stop(conn: sqlite3.Connection) -> dict:
    worker.set_run_state(conn, "apply", status="stopped", current_job_id=None)
    store.log(conn, None, "run_stopped")
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
