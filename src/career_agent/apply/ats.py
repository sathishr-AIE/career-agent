import json
import logging
import sqlite3
from typing import Awaitable, Callable

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
                 filler: Callable[[str], Awaitable[dict]] | None = None) -> dict:
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

    if dry_run:
        answers = await filler(row["url"])
        conn.execute(
            "INSERT INTO application (job_id, resume_version, answers, status)"
            " VALUES (?, ?, ?, 'draft')",
            (job_id, RESUME_VERSION, json.dumps(answers)))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    cur = conn.execute(
        "INSERT INTO application (job_id, resume_version, status, started_at)"
        " VALUES (?, ?, 'in_flight', datetime('now'))", (job_id, RESUME_VERSION))
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
