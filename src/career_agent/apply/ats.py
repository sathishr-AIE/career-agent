import datetime as dt
import json
import logging
import sqlite3
from typing import Awaitable, Callable

from pydantic import BaseModel

log = logging.getLogger(__name__)

RESUME_VERSION = "base-v1"
MAX_ATTEMPTS = 3
BLOCKING = ("in_flight", "submitted", "held_unknown", "failed_permanent")

# Auto-submission is v3, gated on tailoring plus outcome evidence, and Naukri
# submission is not planned at all -- see the Deferred section of
# docs/superpowers/specs/2026-08-18-career-agent-v1-design.md. Until a real
# filler exists, _default_filler opens the page and returns a hardcoded dict
# without touching the form, so a real send would mark the row 'submitted'
# having sent nothing. That false row is not merely cosmetic: it lands in the
# callback-rate denominator, and derive_no_response would later stamp it
# 'no_response'. Callback data is exactly the evidence the v1 -> v2 gate turns
# on, so poisoning it costs more than the missing feature does.
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
    answered directly from CandidateProfile before resolve_answers runs."""
    label: str
    locator: str


QA_VOLATILE_WINDOW_DAYS = 30


def _confirmed_within_days(last_confirmed_at: str, days: int) -> bool:
    """last_confirmed_at is a SQLite datetime('now') string: naive, UTC.
    Comparing against dt.datetime.utcnow() (also naive UTC) keeps both
    sides in the same clock -- this project has hit local-vs-UTC datetime
    mismatches as a recurring bug before, so this stays naive-UTC on
    purpose rather than using a timezone-aware "now"."""
    confirmed = dt.datetime.strptime(last_confirmed_at, "%Y-%m-%d %H:%M:%S")
    return (dt.datetime.utcnow() - confirmed) <= dt.timedelta(days=days)


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


async def _default_filler(url: str) -> dict:
    """Drive the real form. Imported lazily so tests never need a browser."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto(url)
        if await page.locator("iframe[src*='recaptcha'], .h-captcha").count():
            await page.screenshot(path=f"screenshots/captcha-{abs(hash(url))}.png")
            await browser.close()
            raise CaptchaEncountered(url)
        answers = {"note": "filled from facts store and qa_bank"}
        await browser.close()
        return answers


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
                 filler: Callable[[str], Awaitable[dict]] | None = None,
                 resume_version: str | None = None) -> dict:
    # Refuse a real send on the stub filler, before anything is written. An
    # injected filler means a caller supplied a real one (or a test double),
    # so it is allowed through; only the _default_filler path is blocked.
    if not dry_run and filler is None and not SUBMISSION_IMPLEMENTED:
        # `unsupported` marks this refusal categorical, not transient: no
        # retry can make it succeed, so callers can stop waiting on this job.
        return {"ok": False, "unsupported": True, "reason":
                "Submission is not implemented yet (planned for v3; Naukri "
                "never). Apply on the site yourself, then record the outcome."}

    live = conn.execute(
        f"SELECT status FROM application WHERE job_id = ? AND status IN "
        f"({','.join('?' * len(BLOCKING))})", (job_id, *BLOCKING)).fetchone()
    if live:
        return {"ok": False,
                "reason": f"job {job_id} already has a {live['status']} attempt"}

    row = conn.execute("SELECT url FROM job WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason": f"job {job_id} not found"}

    filler = filler or _default_filler
    resume_version = resume_version or RESUME_VERSION

    if dry_run:
        answers = await filler(row["url"])
        conn.execute(
            "INSERT INTO application (job_id, resume_version, answers, status)"
            " VALUES (?, ?, ?, 'draft')",
            (job_id, resume_version, json.dumps(answers)))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    cur = conn.execute(
        "INSERT INTO application (job_id, resume_version, status, started_at)"
        " VALUES (?, ?, 'in_flight', datetime('now'))", (job_id, resume_version))
    app_id = cur.lastrowid
    conn.commit()

    try:
        # ponytail: re-runs the filler independently instead of reusing the
        # draft's stored answers; harmless while _default_filler always
        # returns the same static dict regardless of dry_run, but once the
        # filler is real this needs to read the draft's `answers` column so
        # Send commits exactly what the human reviewed, not a fresh fill.
        answers = await filler(row["url"])
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
