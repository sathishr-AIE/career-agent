import argparse
import asyncio
import logging
import os
from pathlib import Path

import httpx
from apify_client import ApifyClient
from dotenv import load_dotenv

from career_agent import db, discovery, gate, hardfilter, store
from career_agent.config import load_boards, load_brief

log = logging.getLogger(__name__)

MODEL_ID = "claude-agent-sdk"


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


async def _ask(prompt: str) -> str:
    """One tool-less call for gate scoring."""
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                  TextBlock, query)

    chunks = []
    async for message in query(prompt=prompt,
                               options=ClaudeAgentOptions(tools=None)):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
    return "".join(chunks)


async def run_once(args) -> None:
    verify_auth()

    conn = db.connect(Path(args.db))
    db.init_schema(conn)

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

    # 4-5. hard filter, then score survivors
    scored = skipped = 0
    for row in store.unscored_jobs(conn, gate.PROMPT_VERSION, args.max_score):
        job = _row_to_job(row)

        reason = hardfilter.check(job, brief)
        if reason:
            store.save_hard_skip(conn, row["id"], reason)
            skipped += 1
            continue

        verdict = await gate.score(job, brief, store.facts(conn), _ask)
        store.save_assessment(conn, row["id"], verdict, MODEL_ID,
                              gate.PROMPT_VERSION)
        scored += 1

    log.info("hard-filtered %d, scored %d", skipped, scored)


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
    parser.add_argument("--max-score", type=int, default=25, dest="max_score",
                        help="cap scored jobs per run; the rest carry to the "
                             "next run, which keeps the prompt cache warm")
    # Deliberately no --location flag. Search locations come from the brief.
    args = parser.parse_args()

    if args.command == "serve":
        import uvicorn
        uvicorn.run("career_agent.web.app:app", port=8000, reload=False)
    else:
        asyncio.run(run_once(args))
