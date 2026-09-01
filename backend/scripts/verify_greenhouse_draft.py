"""Manual verification script -- NOT part of the pytest suite (this repo
deliberately keeps real-browser-vs-real-ATS testing out of CI, see
CLAUDE.md's testing conventions).

Exercises the real Greenhouse draft filler (apply.ats.submit(dry_run=True))
against real jobs already in the DB, the exact same call do_apply makes when
the dashboard's Apply button is clicked. Opens a visible browser per job,
reads the live form, and prints what it resolved -- SUBMISSION_IMPLEMENTED
stays False throughout, so nothing is ever sent.

Usage (from backend/, after `career-agent run` has scraped fresh jobs):
    python scripts/verify_greenhouse_draft.py [--limit N] [--job-id ID]
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from career_agent import db
from career_agent.apply import ats as ats_apply
from career_agent.config import load_brief, load_candidate_profile

BRIEF_PATH = Path("career_brief.toml")
PROFILE_PATH = Path("candidate_profile.toml")
DB_PATH = Path("data/career.db")


def _select_jobs(conn, job_id: int | None, limit: int):
    if job_id is not None:
        return conn.execute(
            "SELECT id, company, title FROM job WHERE id = ?", (job_id,)).fetchall()
    return conn.execute(
        "SELECT id, company, title FROM job"
        " WHERE source = 'ats'"
        "   AND id NOT IN (SELECT job_id FROM application)"
        " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--job-id", type=int, default=None)
    args = parser.parse_args()

    brief = load_brief(BRIEF_PATH)
    profile = load_candidate_profile(PROFILE_PATH)
    conn = db.connect(DB_PATH)
    db.init_schema(conn)

    jobs = _select_jobs(conn, args.job_id, args.limit)
    if not jobs:
        print("No undrafted Greenhouse jobs found -- run `career-agent run` first.")
        return 0

    for job in jobs:
        print(f"\n{job['company']} -- {job['title']} (job_id={job['id']})")
        result = await ats_apply.submit(conn, job["id"], dry_run=True,
                                        brief=brief, profile=profile)
        if result.get("ok"):
            app = conn.execute(
                "SELECT answers FROM application WHERE job_id = ?"
                " ORDER BY id DESC LIMIT 1", (job["id"],)).fetchone()
            answers = json.loads(app["answers"]) if app["answers"] else {}
            print(f"  drafted -- {len(answers)} field(s) resolved: {list(answers)}")
        elif result.get("needs_answer"):
            print(f"  needs_answer: {result['needs_answer']}")
        elif result.get("held"):
            print(f"  held (captcha) -- {result['reason']}")
        else:
            print(f"  error: {result['reason']}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
