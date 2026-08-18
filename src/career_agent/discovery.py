import logging
from dataclasses import dataclass
from typing import Any

from career_agent.config import Board, CareerBrief
from career_agent.models import Job
from career_agent.sources import apify, ats

log = logging.getLogger(__name__)

MAX_JOBS_PER_QUERY = 50

APIFY_SOURCES = ("linkedin", "naukri")


@dataclass(frozen=True)
class Query:
    source: str
    keyword: str
    location: str
    remote: bool


def plan_queries(brief: CareerBrief) -> list[Query]:
    """One query per title, per search location, per Apify source.

    Location comes from the brief, never from a command-line flag. Filtering
    after the fact is not the same as searching: without this, a run pays for
    listings the hard filter is guaranteed to discard.

    Remote is not a city. It is a separate parameter on both Actors, so
    remote_ok adds a second pass per title rather than sending the string
    "Remote" as a location.
    """
    queries = []
    for source in APIFY_SOURCES:
        for title in brief.target_titles:
            for location in brief.search_locations:
                queries.append(Query(source, title, location, remote=False))
                if brief.remote_ok:
                    queries.append(Query(source, title, location, remote=True))
    return queries


def run_discovery(brief: CareerBrief, boards: list[Board],
                  apify_client: Any, http_client: Any) -> list[Job]:
    jobs: list[Job] = []

    for q in plan_queries(brief):
        fetch = (apify.fetch_linkedin if q.source == "linkedin"
                 else apify.fetch_naukri)
        found = fetch(q.keyword, q.location, q.remote,
                      MAX_JOBS_PER_QUERY, apify_client)
        log.info("%s %r in %s remote=%s -> %d",
                 q.source, q.keyword, q.location, q.remote, len(found))
        jobs.extend(found)

    for board in boards:
        found = ats.fetch_greenhouse(board, http_client)
        log.info("ats %s -> %d", board.company, len(found))
        jobs.extend(found)

    return jobs
