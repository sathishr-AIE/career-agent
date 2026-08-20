import argparse
import asyncio
import functools
import logging
import os
from pathlib import Path

import httpx
from apify_client import ApifyClient
from dotenv import load_dotenv

from career_agent import db, discovery, gate, hardfilter, outcomes, store
from career_agent.config import (DEFAULT_SCORING_MODEL, SCORING_MODELS,
                                 load_boards, load_brief)

log = logging.getLogger(__name__)


class AuthError(RuntimeError):
    pass


def verify_auth() -> None:
    """Fail loudly rather than silently falling back to pay-per-token."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        raise AuthError(
            "ANTHROPIC_API_KEY is set. It outranks the subscription token and "
            "would silently bill per token. Unset it and re-run.")
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        raise AuthError(
            "CLAUDE_CODE_OAUTH_TOKEN is not set. Run `claude setup-token` and "
            "put the result in .env")


async def _ask(prompt: str, model: str | None = None) -> str:
    """One call for gate scoring, with the CLI's default tool set.

    `tools=None` is NOT "no tools" -- it means "don't override", so the SDK
    hands Claude its usual built-ins. `tools=[]` is what actually disables
    them. Scoring only ever needs the text back, so the default set is
    harmless here; the wording is what was wrong, not the argument."""
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                  TextBlock, query)

    chunks = []
    async for message in query(prompt=prompt,
                               options=ClaudeAgentOptions(tools=None,
                                                          model=model)):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
    return "".join(chunks)


async def run_once(args) -> None:
    verify_auth()

    conn = db.connect(Path(args.db))
    db.init_schema(conn)

    settings = store.get_settings(conn)
    scoring_model = settings["scoring_model"]
    if scoring_model not in SCORING_MODELS:
        # Retiring a model leaves stored rows (and db.py's schema default)
        # holding an id save_settings would now reject. Sending it anyway
        # would fail every scoring call; refusing to run would strand the
        # user with no way in but SQL. Fall back and say so.
        log.warning("stored scoring model %r is not a known model; falling "
                    "back to %s. Pick one in Settings to silence this.",
                    scoring_model, DEFAULT_SCORING_MODEL)
        scoring_model = DEFAULT_SCORING_MODEL
    # An explicit --max-score overrides the stored setting for THIS RUN only
    # and is not persisted; omitting it uses the Settings value.
    max_score = (args.max_score if getattr(args, "max_score", None) is not None
                 else settings["max_score_per_run"])
    ask = functools.partial(_ask, model=scoring_model)

    if max_score == 0:
        log.warning("scoring cap is 0: scoring is disabled this run; only "
                    "discovery and the hard filter will run")

    brief = load_brief(Path(args.brief))
    boards = load_boards(Path(args.boards))

    # 1-2. fetch and normalize
    jobs = discovery.run_discovery(
        brief, boards,
        apify_client=ApifyClient(os.environ["APIFY_TOKEN"]),
        http_client=httpx.Client())

    # 3. dedupe and drop stale
    new_count = store.upsert_jobs(conn, jobs, brief)
    log.info("discovered %d, %d new after dedupe and staleness",
             len(jobs), new_count)

    # 4-5. hard filter the whole pool, then score as many survivors as the
    # budget allows. The sweep is deterministic, local, free, and PERSISTED,
    # and unscored_jobs already excludes stage='hard' -- so one pass retires
    # every job that can never pass and the next run finds real candidates
    # immediately. Breaking early instead would leave the pool dirty and
    # reproduce the bug on the following run.
    scored = skipped = 0
    for row in store.unscored_jobs(conn, gate.PROMPT_VERSION):
        job = _row_to_job(row)

        reason = hardfilter.check(job, brief)
        if reason:
            store.save_hard_skip(conn, row["id"], reason)
            skipped += 1
            continue

        if scored >= max_score:
            continue  # budget spent; this survivor carries to the next run

        verdict = await gate.score(job, brief, store.facts(conn), ask)
        store.save_assessment(conn, row["id"], verdict, scoring_model,
                              gate.PROMPT_VERSION)
        scored += 1

    log.info("hard-filtered %d, scored %d", skipped, scored)

    derived = outcomes.derive_no_response(conn)
    log.info("derived %d no_response outcome(s)", derived)


def _row_to_job(row):
    from career_agent.models import Job
    return Job(source=row["source"], external_id=row["external_id"],
               company=row["company"], title=row["title"],
               location=row["location"], is_remote=bool(row["is_remote"]),
               comp_min=row["comp_min"], comp_max=row["comp_max"],
               posted_at=row["posted_at"], description=row["description"])


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(prog="career-agent")
    parser.add_argument("command", choices=["run", "serve"])
    parser.add_argument("--db", default="data/career.db")
    parser.add_argument("--brief", default="career_brief.toml")
    parser.add_argument("--boards", default="ats_boards.toml")
    parser.add_argument("--max-score", type=int, default=None, dest="max_score",
                        help="cap model calls this run, overriding the stored "
                             "Settings value; omit to use that value")
    # Deliberately no --location flag. Search locations come from the brief.
    args = parser.parse_args()

    if args.command == "serve":
        import uvicorn
        uvicorn.run("career_agent.web.app:app", port=8000, reload=False)
    else:
        asyncio.run(run_once(args))


# Required. Without it, `python -m career_agent.run` imports this module,
# defines everything, and exits 0 having done nothing. The console script
# would still work, so the failure is invisible until a scheduled task
# "succeeds" every morning without discovering a single job.
if __name__ == "__main__":
    main()
