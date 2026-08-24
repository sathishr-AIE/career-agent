import datetime as dt
import json
import logging
import sqlite3
from pathlib import Path

from pydantic import BaseModel

from career_agent.config import CandidateProfile, CareerBrief
from career_agent.models import Job

log = logging.getLogger(__name__)

RESUME_VERSION = "base-v1"
MAX_ATTEMPTS = 3
BLOCKING = ("in_flight", "submitted", "held_unknown", "failed_permanent")

# Real Greenhouse automation exists (see _default_compute_answers /
# _default_fill_and_submit below), but a real send stays refused until
# this is flipped by hand -- see the "Rollout" section of
# docs/superpowers/specs/2026-08-23-auto-submission-design.md. Flipping it
# has no effect on non-Greenhouse jobs, which submit() refuses
# unconditionally regardless of this flag.
SUBMISSION_IMPLEMENTED = False


class CaptchaEncountered(Exception):
    """The site showed a captcha, so it has already classified this session as
    suspicious. Backing off is cheaper than pushing through."""


class NeedsAnswer(Exception):
    """A form question has no candidate-profile field and no usable
    qa_bank entry. Raised by resolve_answers; callers must park the job
    rather than retry immediately -- see worker.apply_tick's handling."""
    def __init__(self, question: str):
        super().__init__(question)
        self.question = question


class FormField(BaseModel):
    """One custom question read off a live Greenhouse form. Standard
    fields (name/email/phone/resume/links) never appear here -- they're
    answered directly from CandidateProfile before resolve_answers runs.
    locator is a group selector (e.g. "[name='...']") for a radio/checkbox
    question, or an id selector for anything else -- see
    _read_custom_questions and _fill_field, which dispatch on the live
    element rather than on any kind recorded here, since draft time and
    send time are two separate page loads and answers are stored as a
    flat locator->value dict with no room for extra metadata."""
    label: str
    locator: str


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


def resolve_answers(questions: list[FormField], conn) -> dict[str, str]:
    """Pure decision logic, no Playwright: for each custom question, use a
    qa_bank hit if it exists and (when volatile) was confirmed inside the
    reconfirmation window; otherwise raise NeedsAnswer. This project does
    not attempt to answer a question from the facts store automatically
    (see the plan's "Deviations from the spec" note) -- every custom
    question goes through qa_bank only."""
    from career_agent import store  # local: store.py imports this module
                                     # (for RESUME_VERSION), so a top-level
                                     # import here would be circular.
    answers = {}
    for q in questions:
        row = store.qa_lookup(conn, q.label)
        if row is None:
            raise NeedsAnswer(q.label)
        if row["is_volatile"] and not _confirmed_within_days(
                row["last_confirmed_at"], QA_VOLATILE_WINDOW_DAYS):
            raise NeedsAnswer(q.label)
        answers[q.locator] = row["answer"]
    return answers


def _split_name(candidate_name: str) -> tuple[str, str]:
    """Greenhouse always asks for first and last name separately.
    Ponytail: a naive single-space split, wrong for a name with more than
    one middle/last word grouping intent -- fine for the common case,
    revisit if it matters."""
    parts = candidate_name.strip().split(" ", 1)
    return (parts[0], parts[1] if len(parts) > 1 else "")


# Verify these against a real live Greenhouse posting before flipping
# SUBMISSION_IMPLEMENTED -- they were not confirmed against a rendered
# page from this environment, and Greenhouse has iterated its embed
# markup before. This dict is the most likely thing to need fixing, but
# it is not the ONLY unverified assumption in this file: the custom
# question container selector in _read_custom_questions
# (".field:has(label)"), the submit-button selector in
# _default_fill_and_submit ("button[type=submit], input[type=submit]"),
# and the #resume file-upload call are equally unverified against a live
# posting and live outside this dict.
GREENHOUSE_STANDARD_FIELD_SELECTORS = {
    "first_name": "#first_name",
    "last_name": "#last_name",
    "email": "#email",
    "phone": "#phone",
    "resume": "#resume",
    "linkedin_url": "#job_application_urls_linkedin",
    "portfolio_url": "#job_application_urls_portfolio",
}


async def _check_for_captcha(page, url: str) -> None:
    if await page.locator("iframe[src*='recaptcha'], .h-captcha").count():
        await page.screenshot(path=f"screenshots/captcha-{abs(hash(url))}.png")
        raise CaptchaEncountered(url)


async def _read_custom_questions(page) -> list[FormField]:
    """Scan the form for label+input pairs beyond Greenhouse's standard
    fields. Not unit tested -- see the plan's "Deviations from the spec"
    note; verify against a real posting before trusting this.

    A radio/checkbox question is a group of same-`name` inputs, not one
    element with one id -- an id-selector locator would only ever let
    _fill_field see (and .check()) the first option, never the one whose
    value actually matches the stored answer. So a radio/checkbox group's
    FormField.locator is a `[name='...']` group selector instead of an id
    selector; _fill_field narrows it to the right option at fill time.
    <select> and text-like inputs/textareas keep the id selector, which
    already names exactly one element."""
    standard = set(GREENHOUSE_STANDARD_FIELD_SELECTORS.values())
    fields = []
    for container in await page.locator(".field:has(label)").all():
        input_el = container.locator("input, select, textarea").first
        if await input_el.count() == 0:
            continue
        field_id = await input_el.get_attribute("id")
        if not field_id or f"#{field_id}" in standard:
            continue  # a standard field, already answered from the profile
        label_el = container.locator("label").first
        label = (await label_el.inner_text()).strip()
        input_type = (await input_el.get_attribute("type") or "").lower()
        if input_type in ("radio", "checkbox"):
            name = await input_el.get_attribute("name")
            if not name:
                continue  # can't build a group selector; leave unanswered
                          # rather than mis-fill only the first option
            locator = f"[name='{name}']"
        else:
            locator = f"#{field_id}"
        fields.append(FormField(label=label, locator=locator))
    return fields


async def _default_compute_answers(url: str, job, brief, profile,
                                   conn) -> dict:
    """The real Greenhouse filler's read-only half: visit the form, decide
    every answer, and return them WITHOUT clicking anything. Raises
    NeedsAnswer (via resolve_answers) for a question nothing can answer,
    and CaptchaEncountered if the page shows one. profile is required here
    (only reached for job.source == 'ats' -- see submit()'s routing)."""
    if profile is None:
        raise RuntimeError(
            "candidate_profile.toml not found -- see"
            " candidate_profile.toml.example. Required before a Greenhouse"
            " draft or send can run.")

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        try:
            await page.goto(url)
            await _check_for_captcha(page, url)

            first_name, last_name = _split_name(profile.candidate_name)
            sel = GREENHOUSE_STANDARD_FIELD_SELECTORS
            answers = {}
            # Every standard field gets the same existence guard -- a
            # posting missing one of them (e.g. no phone field) must not
            # produce a stored answer whose selector matches nothing, which
            # would time out at real-send time rather than at draft time.
            candidates = {
                sel["first_name"]: first_name,
                sel["last_name"]: last_name,
                sel["email"]: profile.candidate_email,
                sel["phone"]: profile.candidate_phone,
                sel["linkedin_url"]: profile.linkedin_url,
                sel["portfolio_url"]: profile.portfolio_url,
            }
            for selector, value in candidates.items():
                if value and await page.locator(selector).count():
                    answers[selector] = value

            questions = await _read_custom_questions(page)
            answers.update(resolve_answers(questions, conn))
            return answers
        finally:
            await browser.close()


async def _fill_field(page, locator: str, value) -> None:
    """Dispatch by the live element's actual kind, not by anything recorded
    at draft time (draft time and send time are two separate page loads).
    Playwright's .fill() only works on a text-like <input>/<textarea>; it
    raises on <select> and only ever touches the first element of a
    radio/checkbox group, never the one matching the stored answer -- so
    calling it unconditionally on every answer (the old behavior) would
    error or silently mis-fill as soon as a form has one of those."""
    field = page.locator(locator).first
    tag = await field.evaluate("el => el.tagName.toLowerCase()")
    if tag == "select":
        await field.select_option(str(value))
        return
    input_type = (await field.get_attribute("type") or "").lower()
    if input_type in ("radio", "checkbox"):
        # locator is a `[name='...']` group selector here (see
        # _read_custom_questions); narrow it to the option whose value
        # matches the stored answer before checking it.
        await page.locator(f"{locator}[value={json.dumps(str(value))}]").check()
        return
    await field.fill(str(value))


async def _default_fill_and_submit(url: str, answers: dict, resume_path) -> None:
    """The real Greenhouse filler's write half: type in answers already
    decided by _default_compute_answers, attach the résumé, and click
    Submit. Makes no decisions of its own."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        try:
            await page.goto(url)
            await _check_for_captcha(page, url)

            for locator, value in answers.items():
                await _fill_field(page, locator, value)
            await page.locator(
                GREENHOUSE_STANDARD_FIELD_SELECTORS["resume"]
            ).set_input_files(str(resume_path))
            await page.locator("button[type=submit], input[type=submit]").first.click()
        finally:
            await browser.close()


async def _generic_stub_compute_answers(url: str, job, brief, profile,
                                        conn) -> dict:
    """Non-Greenhouse jobs (job.source != 'ats'): there is no known form
    structure to read, so this proves the page loads and hands back a
    placeholder -- the same behavior every source got before this file's
    v3 changes (this is _default_filler, renamed to match the other two
    functions' signature and to name what it's actually for now that a
    real filler exists alongside it)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        try:
            await page.goto(url)
            await _check_for_captcha(page, url)
            return {"note": "filled from facts store and qa_bank"}
        finally:
            await browser.close()


def sweep_stale_in_flight(conn: sqlite3.Connection, minutes: int = 15) -> int:
    """A crash during submission leaves in_flight behind. Its true state is
    unknown, so it blocks rather than allowing a possible double send."""
    cur = conn.execute(
        "UPDATE application SET status = 'held_unknown'"
        " WHERE status = 'in_flight'"
        f"  AND started_at < datetime('now', '-{int(minutes)} minutes')")
    conn.commit()
    return cur.rowcount


async def submit(conn: sqlite3.Connection, job_id: int, dry_run: bool,
                 brief: CareerBrief | None = None,
                 profile: CandidateProfile | None = None,
                 compute_answers=None, fill_and_submit=None,
                 resume_version: str | None = None) -> dict:
    row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason": f"job {job_id} not found"}
    is_greenhouse = row["source"] == "ats"
    job = Job(source=row["source"], external_id=row["external_id"],
              company=row["company"], title=row["title"],
              location=row["location"], is_remote=bool(row["is_remote"]),
              comp_min=row["comp_min"], comp_max=row["comp_max"],
              posted_at=row["posted_at"], url=row["url"],
              description=row["description"])

    # Refuse a real send with no injected override, before anything is
    # written. Greenhouse jobs are refused only while SUBMISSION_IMPLEMENTED
    # is False (the manual, later "trust it" decision -- see the spec's
    # Rollout section). Every other source is refused unconditionally: no
    # filler exists for "some other website" and none is built in this round.
    if not dry_run and fill_and_submit is None:
        if not is_greenhouse:
            return {"ok": False, "unsupported": True, "reason":
                    "Submission automation only exists for Greenhouse jobs"
                    " today. Apply on the site yourself, then record the"
                    " outcome."}
        if not SUBMISSION_IMPLEMENTED:
            return {"ok": False, "unsupported": True, "reason":
                    "The Greenhouse filler is built but not enabled yet."
                    " Apply on the site yourself, then record the outcome."}

    live = conn.execute(
        f"SELECT status FROM application WHERE job_id = ? AND status IN "
        f"({','.join('?' * len(BLOCKING))})", (job_id, *BLOCKING)).fetchone()
    if live:
        return {"ok": False,
                "reason": f"job {job_id} already has a {live['status']} attempt"}

    resume_version = resume_version or RESUME_VERSION

    def _default_compute_fn():
        return _default_compute_answers if is_greenhouse else _generic_stub_compute_answers

    if dry_run:
        compute_fn = compute_answers or _default_compute_fn()
        try:
            answers = await compute_fn(row["url"], job, brief, profile, conn)
        except CaptchaEncountered as exc:
            conn.execute("INSERT INTO event (job_id, type, payload)"
                         " VALUES (?, 'captcha_held', ?)", (job_id, str(exc)))
            conn.commit()
            return {"ok": False, "held": True,
                    "reason": "captcha encountered; held for review"}
        except NeedsAnswer as exc:
            return {"ok": False, "needs_answer": exc.question,
                    "reason": f"needs an answer: {exc.question}"}
        conn.execute(
            "INSERT INTO application (job_id, resume_version, answers, status)"
            " VALUES (?, ?, ?, 'draft')",
            (job_id, resume_version, json.dumps(answers)))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    # Real send: reuse the draft's own answers rather than recomputing --
    # what a human reviewed (or what auto mode drafted a moment earlier) is
    # exactly what must be sent. Only falls back to computing fresh when
    # submit() is called directly with no draft on record (not reachable
    # through apply_tick's normal path, but submit() stays safe to call
    # this way).
    draft = conn.execute(
        "SELECT answers FROM application WHERE job_id = ? AND status = 'draft'"
        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    send_fn = fill_and_submit or _default_fill_and_submit
    if draft is not None and draft["answers"] is not None:
        # Reused verbatim, with no re-check of qa_bank's 30-day volatility
        # window (resolve_answers's QA_VOLATILE_WINDOW_DAYS) even if the
        # draft is older than that window and a volatile answer it used has
        # since gone stale. This is a real tension, not an oversight:
        # revalidating volatility here would contradict the whole point of
        # reuse -- "what a human reviewed (or what auto mode drafted a
        # moment ago) is exactly what gets sent" -- by silently sending
        # different answers than what was shown for review. Reuse wins.
        answers = json.loads(draft["answers"])
    elif compute_answers is not None:
        # No draft on record, but the caller supplied its own
        # compute_answers -- honor it rather than silently sending nothing.
        try:
            answers = await compute_answers(row["url"], job, brief, profile, conn)
        except CaptchaEncountered as exc:
            conn.execute("INSERT INTO event (job_id, type, payload)"
                         " VALUES (?, 'captcha_held', ?)", (job_id, str(exc)))
            conn.commit()
            return {"ok": False, "held": True,
                    "reason": "captcha encountered; held for review"}
        except NeedsAnswer as exc:
            return {"ok": False, "needs_answer": exc.question,
                    "reason": f"needs an answer: {exc.question}"}
    else:
        # No draft and no caller override: apply_tick's normal flow always
        # drafts before it sends, so this is only reached by a caller that
        # invokes submit(dry_run=False) directly. Silently launching the
        # real Greenhouse/generic filler here -- a live browser, needing a
        # profile the caller never supplied -- would be a surprising side
        # effect of a missing draft, not a safety net, so this sends with
        # no extra answers rather than guessing which default to run.
        answers = {}

    resume_row = conn.execute("SELECT path FROM resume WHERE version = ?",
                              (resume_version,)).fetchone()
    if resume_row is None:
        return {"ok": False,
                "reason": f"no résumé on record for version {resume_version!r}"}

    cur = conn.execute(
        "INSERT INTO application (job_id, resume_version, status, started_at)"
        " VALUES (?, ?, 'in_flight', datetime('now'))", (job_id, resume_version))
    app_id = cur.lastrowid
    conn.commit()

    try:
        await send_fn(row["url"], answers, Path(resume_row["path"]))
    except CaptchaEncountered as exc:
        conn.execute("DELETE FROM application WHERE id = ?", (app_id,))
        conn.execute("INSERT INTO event (job_id, type, payload)"
                     " VALUES (?, 'captcha_held', ?)", (job_id, str(exc)))
        conn.commit()
        return {"ok": False, "held": True,
                "reason": "captcha encountered; held for review"}
    except Exception as exc:
        failures = conn.execute(
            "SELECT COUNT(*) n FROM application"
            " WHERE job_id = ? AND status = 'failed'", (job_id,)).fetchone()["n"]
        status = "failed_permanent" if failures + 1 >= MAX_ATTEMPTS else "failed"
        conn.execute("UPDATE application SET status = ? WHERE id = ?",
                     (status, app_id))
        conn.execute("INSERT INTO event (job_id, type, payload) VALUES (?, ?, ?)",
                     (job_id, status, str(exc)))
        conn.commit()
        return {"ok": False, "reason": f"submission {status}: {exc}"}

    conn.execute(
        "UPDATE application SET status = 'submitted', answers = ?,"
        " submitted_at = datetime('now') WHERE id = ?",
        (json.dumps(answers), app_id))
    conn.execute("INSERT INTO event (job_id, type, payload)"
                 " VALUES (?, 'submitted', ?)", (job_id, row["url"]))
    conn.commit()
    return {"ok": True, "job_id": job_id, "status": "submitted"}
